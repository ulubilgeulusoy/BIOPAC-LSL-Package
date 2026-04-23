# -*- coding: utf-8 -*-
"""
BIOPAC (AcqKnowledge NDT) -> LSL streamer with a Tkinter GUI.

This version focuses on runtime robustness:
- no terminal prompts
- bounded buffering between BIOPAC callbacks and LSL sending
- watchdog monitoring for stale data / stalled pushes
- best-effort reconnect when BIOPAC delivery stalls
- live operator status, event log, and simple signal preview
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

import biopacndt
import tkinter as tk
from tkinter import messagebox, ttk

from pylsl import StreamInfo, StreamOutlet, local_clock


SRATE = 500
STREAM_NAME_BASE = "Biopac"
STREAM_TYPE = "PsychoPhys"
DEFAULT_MAPPING_ENV = "BIOPAC_DEFAULT_MAPPING"
FRAME_QUEUE_MAX = 4000
EVENT_LOG_MAX = 500
PREVIEW_POINTS = 300
PREVIEW_SECONDS = 3.0
STATUS_POLL_MS = 250
PREVIEW_POLL_MS = 200
STALE_WARNING_SEC = 1.5
STALE_FAULT_SEC = 4.0
RECONNECT_COOLDOWN_SEC = 5.0

UNITS = {
    "ECG": "microvolts",
    "RSP": "a.u.",
    "EDA": "microsiemens",
}

CHANNEL_ORDER = ["ECG", "RSP", "EDA"]
ANALOG_CHANNEL_COUNT = 16


def get_time_vec() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S%f")


LOG_FILENAME = f"bp_ecg_rsp_eda_{get_time_vec()}.log"
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    filename=LOG_FILENAME,
)
logger = logging.getLogger(__name__)


@dataclass
class EngineSnapshot:
    state: str
    state_detail: str
    connected: bool
    acquisition_on: Optional[bool]
    enabled_channels: List[str]
    mapping: Dict[str, int]
    stream_name: str
    session_id: str
    samples_pushed: int
    input_frames_received: int
    dropped_input_frames: int
    reconnect_attempts: int
    queue_depth: int
    last_frame_age: Optional[float]
    last_push_age: Optional[float]
    streaming: bool
    log_filename: str
    active_labels: List[str]


class BiopacLSLEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._event_lock = threading.Lock()
        self._status_callbacks: List[Callable[[EngineSnapshot], None]] = []
        self._event_callbacks: List[Callable[[str], None]] = []

        self.state = "DISCONNECTED"
        self.state_detail = "Not connected"

        self.acq_server = None
        self.data_server = None
        self.enabled_channels = []
        self.stream_outlet = None
        self.stream_info = None

        self.mapping: Dict[str, int] = {}
        self.channel_labels: List[str] = []
        self.stream_name = ""
        self.session_id = ""

        self.frame_queue: "queue.Queue[Tuple[float, List[float]]]" = queue.Queue(maxsize=FRAME_QUEUE_MAX)
        self.stop_event = threading.Event()
        self.stream_thread: Optional[threading.Thread] = None
        self.monitor_thread: Optional[threading.Thread] = None

        self.acquisition_state: Optional[bool] = None
        self.samples_pushed = 0
        self.input_frames_received = 0
        self.dropped_input_frames = 0
        self.reconnect_attempts = 0
        self.last_frame_time: Optional[float] = None
        self.last_push_time: Optional[float] = None
        self.last_reconnect_time = 0.0
        self.reconnect_in_progress = False

        self.latest_frame: Optional[List[float]] = None
        self.preview_history = {label: deque(maxlen=PREVIEW_POINTS * 4) for label in CHANNEL_ORDER}
        self.events = deque(maxlen=EVENT_LOG_MAX)

    def subscribe_status(self, callback: Callable[[EngineSnapshot], None]) -> None:
        self._status_callbacks.append(callback)

    def subscribe_event(self, callback: Callable[[str], None]) -> None:
        self._event_callbacks.append(callback)

    def _set_state(self, state: str, detail: str) -> None:
        with self._lock:
            self.state = state
            self.state_detail = detail
        logger.info("State -> %s: %s", state, detail)
        self.log_event(f"{state}: {detail}")
        self._notify_status()

    def log_event(self, message: str) -> None:
        timestamped = f"{datetime.now().strftime('%H:%M:%S')}  {message}"
        logger.info(message)
        with self._event_lock:
            self.events.append(timestamped)
        for callback in self._event_callbacks:
            callback(timestamped)

    def _notify_status(self) -> None:
        snapshot = self.get_snapshot()
        for callback in self._status_callbacks:
            callback(snapshot)

    def get_snapshot(self) -> EngineSnapshot:
        now = time.monotonic()
        with self._lock:
            last_frame_age = None if self.last_frame_time is None else max(0.0, now - self.last_frame_time)
            last_push_age = None if self.last_push_time is None else max(0.0, now - self.last_push_time)
            enabled = [self._format_channel_line(i, ch) for i, ch in enumerate(self.enabled_channels)]
            return EngineSnapshot(
                state=self.state,
                state_detail=self.state_detail,
                connected=self.acq_server is not None,
                acquisition_on=self.acquisition_state,
                enabled_channels=enabled,
                mapping=dict(self.mapping),
                stream_name=self.stream_name,
                session_id=self.session_id,
                samples_pushed=self.samples_pushed,
                input_frames_received=self.input_frames_received,
                dropped_input_frames=self.dropped_input_frames,
                reconnect_attempts=self.reconnect_attempts,
                queue_depth=self.frame_queue.qsize(),
                last_frame_age=last_frame_age,
                last_push_age=last_push_age,
                streaming=self.is_streaming(),
                log_filename=LOG_FILENAME,
                active_labels=list(self.channel_labels),
            )

    def _format_channel_line(self, index: int, channel) -> str:
        label = self.describe_channel(channel)
        divider = getattr(channel, "SamplingDivider", "?")
        return f"[{index}] {label} | divider={divider}"

    @staticmethod
    def describe_channel(channel) -> str:
        channel_type = str(getattr(channel, "Type", "unknown")).lower()
        channel_index = getattr(channel, "Index", None)

        if isinstance(channel_index, int):
            human_number = channel_index + 1
            if channel_type == "analog":
                return f"A{human_number} / Channel {human_number}"
            return f"{channel_type.title()} {human_number}"

        return str(channel)

    def resolve_analog_channel_to_delivery_index(self, analog_channel_number: int) -> int:
        analog_index = analog_channel_number - 1
        for delivery_index, channel in enumerate(self.enabled_channels):
            if str(getattr(channel, "Type", "")).lower() != "analog":
                continue
            if getattr(channel, "Index", None) == analog_index:
                return delivery_index
        raise ValueError(f"A{analog_channel_number} is not currently enabled in AcqKnowledge")

    def is_streaming(self) -> bool:
        return self.stream_thread is not None and self.stream_thread.is_alive()

    def connect(self) -> None:
        if self.is_streaming():
            self.stop_streaming()
        with self._lock:
            self._disconnect_locked(stop_state=False)
            self._set_state("CONNECTING", "Connecting to AcqKnowledge")
            try:
                self.acq_server = biopacndt.AcqNdtQuickConnect()
                self.enabled_channels = self.acq_server.DeliverAllEnabledChannels()
                self.acquisition_state = self._query_acquisition_state_locked()
                single_port = self.acq_server.getSingleConnectionModePort()

                self.data_server = biopacndt.AcqNdtDataServer(single_port, self.enabled_channels)
                self.data_server.RegisterCallback("OutputData", self._handle_acquired_data)
                self.data_server.RegisterCloseCallback("Closed", self._handle_data_server_closed)
                self.data_server.Start()
            except Exception as exc:
                self.acq_server = None
                self.data_server = None
                detail = f"Connection failed: {exc}"
                logger.exception(detail)
                self._set_state("FAULT", detail)
                raise

        self.log_event(f"Connected to AcqKnowledge with {len(self.enabled_channels)} enabled channels")
        self._set_state("CONNECTED", "Connected and receiving data server callbacks")

    def refresh_channels(self) -> None:
        with self._lock:
            if self.acq_server is None:
                raise RuntimeError("Not connected")
            self.enabled_channels = self.acq_server.DeliverAllEnabledChannels()
        self.log_event(f"Refreshed channels: {len(self.enabled_channels)} enabled")
        self._notify_status()

    def set_mapping(self, mapping: Dict[str, int], session_id: str) -> None:
        self._validate_mapping(mapping)
        with self._lock:
            self.mapping = dict(mapping)
            self.channel_labels = [label for label in CHANNEL_ORDER if label in mapping]
            self.session_id = session_id.strip()
            suffix = "-".join(self.channel_labels)
            if self.session_id:
                self.stream_name = f"{STREAM_NAME_BASE} {suffix} ({self.session_id})"
            else:
                self.stream_name = f"{STREAM_NAME_BASE} {suffix}"
            mapping_summary = ", ".join(
                f"{label} -> {self.describe_channel(self.enabled_channels[idx])} [delivery {idx}]"
                for label, idx in mapping.items()
            )
        self.log_event(f"Configured mapping: {mapping_summary}")
        self._set_state("READY", "Mapping configured and ready to stream")

    def _validate_mapping(self, mapping: Dict[str, int]) -> None:
        if not mapping:
            raise ValueError("Select at least one mapped signal")

        if len(set(mapping.values())) != len(mapping.values()):
            raise ValueError("Each selected signal must use a different channel index")

        enabled_count = len(self.enabled_channels)
        for label, idx in mapping.items():
            if label not in CHANNEL_ORDER:
                raise ValueError(f"Unknown signal label: {label}")
            if idx < 0 or idx >= enabled_count:
                raise ValueError(f"{label} is mapped to index {idx}, but only {enabled_count} enabled channels exist")

    def set_acquisition(self, desired_on: bool) -> None:
        with self._lock:
            if self.acq_server is None:
                raise RuntimeError("Not connected")

            current = self._get_acquisition_state_locked()
            if current is None:
                self.log_event("Acquisition state unavailable; sending best-effort toggle")
                self.acq_server.toggleAcquisition()
            elif current != desired_on:
                self.acq_server.toggleAcquisition()
            else:
                self.log_event(f"Acquisition already {'ON' if desired_on else 'OFF'}")
                self._notify_status()
                return

        time.sleep(0.5)
        with self._lock:
            self.acquisition_state = self._query_acquisition_state_locked()
        target = "ON" if desired_on else "OFF"
        self.log_event(f"Requested acquisition {target}")
        self._notify_status()

    def _get_acquisition_state_locked(self) -> Optional[bool]:
        return self.acquisition_state

    def _query_acquisition_state_locked(self) -> Optional[bool]:
        if self.acq_server is None:
            return None
        try:
            return bool(self.acq_server.getAcquisitionInProgress())
        except Exception:
            return None

    def start_streaming(self) -> None:
        with self._lock:
            if self.acq_server is None:
                raise RuntimeError("Connect to AcqKnowledge first")
            if not self.mapping:
                raise RuntimeError("Set a valid mapping first")
            if self.is_streaming():
                raise RuntimeError("Streaming is already active")

            self._drain_queue_locked()
            self.samples_pushed = 0
            self.input_frames_received = 0
            self.dropped_input_frames = 0
            self.last_frame_time = None
            self.last_push_time = None
            self.latest_frame = None
            self.stop_event = threading.Event()
            for history in self.preview_history.values():
                history.clear()

            source_id = f"biopac_{'_'.join(self.channel_labels).lower()}_{get_time_vec()}"
            self.stream_info = StreamInfo(
                self.stream_name,
                STREAM_TYPE,
                len(self.channel_labels),
                SRATE,
                "float32",
                source_id,
            )
            self.stream_info.desc().append_child_value("manufacturer", "Biopac")
            chns = self.stream_info.desc().append_child("channels")
            for label in self.channel_labels:
                channel = chns.append_child("channel")
                channel.append_child_value("label", label)
                channel.append_child_value("unit", UNITS.get(label, "a.u."))
                channel.append_child_value("type", label)

            self.stream_outlet = StreamOutlet(self.stream_info)
            self.stream_thread = threading.Thread(target=self._stream_loop, name="LSLStreamThread", daemon=True)
            self.monitor_thread = threading.Thread(target=self._monitor_loop, name="LSLMonitorThread", daemon=True)
            self.stream_thread.start()
            self.monitor_thread.start()

        self._set_state("STREAMING", "Streaming data to LSL")

    def stop_streaming(self) -> None:
        with self._lock:
            was_streaming = self.is_streaming() or self.stream_outlet is not None
            self.stop_event.set()
            stream_thread = self.stream_thread
            monitor_thread = self.monitor_thread
            self.stream_thread = None
            self.monitor_thread = None
            self.stream_outlet = None
            self.stream_info = None

        for thread in (stream_thread, monitor_thread):
            if thread and thread.is_alive():
                thread.join(timeout=2.0)

        if not was_streaming:
            return

        self.log_event("Streaming stopped")
        if self.acq_server is None:
            self._set_state("DISCONNECTED", "Disconnected")
        else:
            self._set_state("CONNECTED", "Connected but not streaming")

    def shutdown(self) -> None:
        with self._lock:
            self.stop_event.set()
        self.stop_streaming()
        with self._lock:
            self._disconnect_locked(stop_state=True)

    def _disconnect_locked(self, stop_state: bool) -> None:
        if self.data_server is not None:
            try:
                self.data_server.Stop()
            except Exception:
                logger.exception("Error stopping data server")
        self.data_server = None
        self.acq_server = None
        self.enabled_channels = []
        self.acquisition_state = None
        self.mapping = {}
        self.channel_labels = []
        self.stream_name = ""
        self.latest_frame = None
        self._drain_queue_locked()
        if stop_state:
            self._set_state("DISCONNECTED", "Disconnected")

    def _drain_queue_locked(self) -> None:
        while True:
            try:
                self.frame_queue.get_nowait()
            except queue.Empty:
                break

    def _handle_acquired_data(self, hardware_index, frame, channels_in_slice) -> None:
        timestamp = local_clock()
        frame_list = [float(value) for value in frame]

        with self._lock:
            self.input_frames_received += 1
            now = time.monotonic()
            self.last_frame_time = now
            self.latest_frame = frame_list
            for label, idx in self.mapping.items():
                if idx < len(frame_list):
                    self.preview_history[label].append((now, frame_list[idx]))
                else:
                    self.preview_history[label].append((now, float("nan")))

        try:
            self.frame_queue.put_nowait((timestamp, frame_list))
        except queue.Full:
            try:
                self.frame_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.frame_queue.put_nowait((timestamp, frame_list))
            except queue.Full:
                pass
            with self._lock:
                self.dropped_input_frames += 1

        if self.input_frames_received == 1:
            self.log_event("First BIOPAC frame received")

    def _handle_data_server_closed(self) -> None:
        self.log_event("BIOPAC data server connection closed")

    def _build_sample(self, frame_list: List[float]) -> List[float]:
        sample = []
        for label in self.channel_labels:
            idx = self.mapping[label]
            if idx >= len(frame_list):
                sample.append(float("nan"))
            else:
                sample.append(float(frame_list[idx]))
        return sample

    def _stream_loop(self) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    timestamp, frame_list = self.frame_queue.get(timeout=0.25)
                except queue.Empty:
                    continue

                sample = self._build_sample(frame_list)
                if self.stream_outlet is None:
                    continue

                self.stream_outlet.push_sample(sample, timestamp)
                with self._lock:
                    self.samples_pushed += 1
                    self.last_push_time = time.monotonic()

                if self.samples_pushed == 1:
                    self.log_event(f"First LSL sample pushed: {sample}")
        except Exception as exc:
            detail = f"Streaming error: {exc}"
            logger.exception(detail)
            self._set_state("FAULT", detail)
            self.stop_event.set()

    def _monitor_loop(self) -> None:
        warning_raised = False
        try:
            while not self.stop_event.is_set():
                time.sleep(0.25)
                now = time.monotonic()

                with self._lock:
                    last_frame_age = None if self.last_frame_time is None else now - self.last_frame_time

                if last_frame_age is None:
                    continue

                if last_frame_age >= STALE_WARNING_SEC and not warning_raised:
                    warning_raised = True
                    self._set_state("WARNING", f"No new BIOPAC frames for {last_frame_age:.1f}s")

                if last_frame_age < STALE_WARNING_SEC and self.state == "WARNING":
                    warning_raised = False
                    self._set_state("STREAMING", "Streaming data to LSL")

                if last_frame_age >= STALE_FAULT_SEC:
                    self._attempt_reconnect()
        except Exception as exc:
            detail = f"Monitor error: {exc}"
            logger.exception(detail)
            self._set_state("FAULT", detail)
            self.stop_event.set()

    def _attempt_reconnect(self) -> None:
        with self._lock:
            if self.reconnect_in_progress:
                return
            if time.monotonic() - self.last_reconnect_time < RECONNECT_COOLDOWN_SEC:
                return
            self.reconnect_in_progress = True
            self.last_reconnect_time = time.monotonic()
            self.reconnect_attempts += 1

        self._set_state("WARNING", "Data stalled; attempting reconnect")
        self.log_event("Starting reconnect attempt")

        try:
            with self._lock:
                if self.data_server is not None:
                    try:
                        self.data_server.Stop()
                    except Exception:
                        logger.exception("Error stopping data server during reconnect")
                    self.data_server = None

                if self.acq_server is None:
                    self.acq_server = biopacndt.AcqNdtQuickConnect()

                self.enabled_channels = self.acq_server.DeliverAllEnabledChannels()
                single_port = self.acq_server.getSingleConnectionModePort()
                self.data_server = biopacndt.AcqNdtDataServer(single_port, self.enabled_channels)
                self.data_server.RegisterCallback("OutputData", self._handle_acquired_data)
                self.data_server.RegisterCloseCallback("Closed", self._handle_data_server_closed)
                self.data_server.Start()
                self._validate_mapping(self.mapping)
                self.last_frame_time = time.monotonic()
        except Exception as exc:
            detail = f"Reconnect failed: {exc}"
            logger.exception(detail)
            self._set_state("FAULT", detail)
            self.stop_event.set()
        else:
            self.log_event("Reconnect successful")
            self._set_state("STREAMING", "Streaming data to LSL")
        finally:
            with self._lock:
                self.reconnect_in_progress = False


class SignalPreview(ttk.Frame):
    def __init__(self, master) -> None:
        super().__init__(master)
        self.canvas = tk.Canvas(self, height=220, background="#0f172a", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.palette = {
            "ECG": "#22c55e",
            "RSP": "#38bdf8",
            "EDA": "#f59e0b",
        }

    def _recent_samples(self, samples: deque, now: float) -> List[Tuple[float, float]]:
        cutoff = now - PREVIEW_SECONDS
        return [(ts, value) for ts, value in samples if ts >= cutoff and value == value]

    def _downsample_for_width(self, samples: List[Tuple[float, float]], width: int) -> List[Tuple[float, float]]:
        if len(samples) <= width:
            return samples

        step = len(samples) / width
        reduced = []
        idx = 0.0
        while int(idx) < len(samples):
            reduced.append(samples[int(idx)])
            idx += step
        if reduced[-1] != samples[-1]:
            reduced.append(samples[-1])
        return reduced

    def _robust_bounds(self, values: List[float]) -> Tuple[float, float]:
        ordered = sorted(values)
        if len(ordered) < 4:
            lower = min(ordered)
            upper = max(ordered)
        else:
            low_idx = int(0.05 * (len(ordered) - 1))
            high_idx = int(0.95 * (len(ordered) - 1))
            lower = ordered[low_idx]
            upper = ordered[high_idx]

        if abs(upper - lower) < 1e-9:
            center = ordered[len(ordered) // 2]
            return center - 1.0, center + 1.0

        padding = (upper - lower) * 0.15
        return lower - padding, upper + padding

    def render(self, history: Dict[str, deque], active_labels: List[str]) -> None:
        self.canvas.delete("all")
        width = max(self.canvas.winfo_width(), 10)
        height = max(self.canvas.winfo_height(), 10)
        now = time.monotonic()

        if not active_labels:
            self.canvas.create_text(
                width / 2,
                height / 2,
                text="No active signals selected",
                fill="#cbd5e1",
                font=("Segoe UI", 12),
            )
            return

        row_height = height / len(active_labels)
        for row, label in enumerate(active_labels):
            top = row * row_height
            bottom = top + row_height
            mid = (top + bottom) / 2
            self.canvas.create_line(0, mid, width, mid, fill="#334155")
            self.canvas.create_text(8, top + 14, text=label, anchor="w", fill="#e2e8f0", font=("Segoe UI", 10, "bold"))

            recent = self._recent_samples(history[label], now)
            if len(recent) < 2:
                self.canvas.create_text(
                    width - 10,
                    top + 14,
                    text="Waiting for live data",
                    anchor="e",
                    fill="#94a3b8",
                    font=("Segoe UI", 9),
                )
                continue

            reduced = self._downsample_for_width(recent, max(width - 40, 50))
            values = [value for _, value in reduced]
            min_val, max_val = self._robust_bounds(values)

            points = []
            start_time = max(now - PREVIEW_SECONDS, reduced[0][0])
            time_span = max(PREVIEW_SECONDS, reduced[-1][0] - start_time, 1e-6)
            for timestamp, value in reduced:
                norm = (value - min_val) / (max_val - min_val)
                norm = min(max(norm, 0.0), 1.0)
                y = bottom - (norm * (row_height - 30)) - 10
                x = ((timestamp - start_time) / time_span) * (width - 20) + 10
                points.extend([x, y])

            if len(points) >= 4:
                self.canvas.create_line(*points, fill=self.palette.get(label, "#ffffff"), width=2)


class BiopacApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("BIOPAC LSL Control Panel")
        self.root.geometry("1200x820")
        self.root.minsize(980, 720)

        self.engine = BiopacLSLEngine()
        self.engine.subscribe_event(self._queue_event)
        self.engine.subscribe_status(self._queue_status)

        self.pending_events: "queue.Queue[str]" = queue.Queue()
        self.pending_status: "queue.Queue[EngineSnapshot]" = queue.Queue()
        self.latest_snapshot = self.engine.get_snapshot()

        default_mapping = os.environ.get(DEFAULT_MAPPING_ENV, "").strip()
        parsed_default = self._parse_default_mapping(default_mapping)

        self.session_var = tk.StringVar()
        self.status_var = tk.StringVar(value="DISCONNECTED")
        self.detail_var = tk.StringVar(value="Not connected")
        self.next_step_var = tk.StringVar(value="Next step: Connect to the prepared AcqKnowledge session.")
        self.connection_var = tk.StringVar(value="No connection")
        self.acquisition_var = tk.StringVar(value="Unknown")
        self.samples_var = tk.StringVar(value="0")
        self.frames_var = tk.StringVar(value="0")
        self.queue_var = tk.StringVar(value="0")
        self.last_frame_var = tk.StringVar(value="--")
        self.last_push_var = tk.StringVar(value="--")
        self.reconnect_var = tk.StringVar(value="0")
        self.log_var = tk.StringVar(value=LOG_FILENAME)
        self.stream_var = tk.StringVar(value="--")

        self.mapping_vars = {}
        self.mapping_combos = {}
        self.channel_option_lookup: Dict[str, int] = {}
        self.pending_default_mapping = parsed_default
        for label in CHANNEL_ORDER:
            default_idx = parsed_default.get(label)
            self.mapping_vars[label] = tk.StringVar(value="Off" if default_idx is None else str(default_idx + 1))

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._schedule_polls()

    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        outer = ttk.Frame(self.root)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(outer, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        main = ttk.Frame(self.canvas, padding=12)
        self.canvas_window = self.canvas.create_window((0, 0), window=main, anchor="nw")
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(2, weight=1)
        main.rowconfigure(3, weight=1)
        self.main_frame = main

        main.bind("<Configure>", self._on_main_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        header = ttk.Label(main, text="BIOPAC LSL Control Panel", font=("Segoe UI", 18, "bold"))
        header.grid(row=0, column=0, columnspan=2, sticky="w")

        banner = ttk.Frame(main, padding=(12, 10))
        banner.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 12))
        banner.columnconfigure(1, weight=1)
        ttk.Label(banner, textvariable=self.status_var, font=("Segoe UI", 16, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(banner, textvariable=self.detail_var, font=("Segoe UI", 11)).grid(row=0, column=1, sticky="w", padx=(16, 0))
        ttk.Label(banner, textvariable=self.next_step_var, font=("Segoe UI", 11, "bold")).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        left_top = ttk.LabelFrame(main, text="Prepared AcqKnowledge Session", padding=12)
        left_top.grid(row=2, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        left_top.columnconfigure(1, weight=1)

        ttk.Label(left_top, text="Session ID").grid(row=0, column=0, sticky="w")
        self.session_entry = ttk.Entry(left_top, textvariable=self.session_var)
        self.session_entry.grid(row=0, column=1, sticky="ew", padx=(10, 0))

        ttk.Label(left_top, text="Connection").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Label(left_top, textvariable=self.connection_var).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=(10, 0))

        ttk.Label(left_top, text="Acquisition").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Label(left_top, textvariable=self.acquisition_var).grid(row=2, column=1, sticky="w", padx=(10, 0), pady=(8, 0))

        ttk.Label(left_top, text="LSL Stream").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Label(left_top, textvariable=self.stream_var).grid(row=3, column=1, sticky="w", padx=(10, 0), pady=(8, 0))

        ttk.Label(left_top, text="Log File").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Label(left_top, textvariable=self.log_var).grid(row=4, column=1, sticky="w", padx=(10, 0), pady=(8, 0))

        button_row = ttk.Frame(left_top)
        button_row.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        for idx in range(5):
            button_row.columnconfigure(idx, weight=1)

        self.connect_button = ttk.Button(button_row, text="Connect", command=self.connect)
        self.connect_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.refresh_button = ttk.Button(button_row, text="Refresh Channels", command=self.refresh_channels)
        self.refresh_button.grid(row=0, column=1, sticky="ew", padx=6)
        self.acq_on_button = ttk.Button(button_row, text="Start Acquisition", command=lambda: self.set_acquisition(True))
        self.acq_on_button.grid(row=0, column=2, sticky="ew", padx=6)
        self.acq_off_button = ttk.Button(button_row, text="Stop Acquisition", command=lambda: self.set_acquisition(False))
        self.acq_off_button.grid(row=0, column=3, sticky="ew", padx=6)
        self.reset_button = ttk.Button(button_row, text="Reset", command=self.reset_engine)
        self.reset_button.grid(row=0, column=4, sticky="ew", padx=(6, 0))

        right_top = ttk.LabelFrame(main, text="Channel Mapping", padding=12)
        right_top.grid(row=2, column=1, sticky="nsew", padx=(6, 0), pady=(0, 6))
        right_top.columnconfigure(1, weight=1)

        ttk.Label(
            right_top,
            text="Map each signal to an AcqKnowledge analog channel. The app resolves it to the active NDT delivery index automatically.",
            wraplength=430,
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        for row, label in enumerate(CHANNEL_ORDER, start=1):
            ttk.Label(right_top, text=label).grid(row=row, column=0, sticky="w", pady=(10 if row == 1 else 8, 0))
            combo = ttk.Combobox(right_top, textvariable=self.mapping_vars[label], state="readonly")
            combo.grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=(10 if row == 1 else 8, 0))
            combo["values"] = ("Off",)
            self.mapping_combos[label] = combo

        self.apply_mapping_button = ttk.Button(right_top, text="Confirm Mapping", command=self.apply_mapping)
        self.apply_mapping_button.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(14, 0))

        channels_frame = ttk.LabelFrame(main, text="Detected Channels", padding=12)
        channels_frame.grid(row=3, column=0, sticky="nsew", padx=(0, 6), pady=(6, 0))
        channels_frame.columnconfigure(0, weight=1)
        channels_frame.rowconfigure(0, weight=1)

        self.channels_list = tk.Listbox(channels_frame, height=10)
        self.channels_list.grid(row=0, column=0, sticky="nsew")
        channels_scroll = ttk.Scrollbar(channels_frame, orient="vertical", command=self.channels_list.yview)
        channels_scroll.grid(row=0, column=1, sticky="ns")
        self.channels_list.configure(yscrollcommand=channels_scroll.set)

        status_frame = ttk.LabelFrame(main, text="Live Status", padding=12)
        status_frame.grid(row=3, column=1, sticky="nsew", padx=(6, 0), pady=(6, 0))
        status_frame.columnconfigure(1, weight=1)
        status_frame.columnconfigure(3, weight=1)

        pairs = [
            ("Samples pushed", self.samples_var),
            ("Input frames", self.frames_var),
            ("Queue depth", self.queue_var),
            ("Last frame age", self.last_frame_var),
            ("Last push age", self.last_push_var),
            ("Reconnect attempts", self.reconnect_var),
        ]
        for idx, (label_text, variable) in enumerate(pairs):
            row = idx // 2
            column = (idx % 2) * 2
            ttk.Label(status_frame, text=label_text).grid(row=row, column=column, sticky="w", pady=4)
            ttk.Label(status_frame, textvariable=variable).grid(row=row, column=column + 1, sticky="w", padx=(8, 18), pady=4)

        control_row = ttk.Frame(status_frame)
        control_row.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        control_row.columnconfigure(0, weight=1)
        control_row.columnconfigure(1, weight=1)
        self.start_stream_button = ttk.Button(control_row, text="Start LSL Streaming", command=self.start_streaming)
        self.start_stream_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.stop_stream_button = ttk.Button(control_row, text="Stop LSL Streaming", command=self.stop_streaming)
        self.stop_stream_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        preview_frame = ttk.LabelFrame(main, text="Signal Preview", padding=12)
        preview_frame.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(12, 6))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self.preview = SignalPreview(preview_frame)
        self.preview.grid(row=0, column=0, sticky="nsew")

        log_frame = ttk.LabelFrame(main, text="Event Log", padding=12)
        log_frame.grid(row=5, column=0, columnspan=2, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        main.rowconfigure(5, weight=1)

        self.log_text = tk.Text(log_frame, height=10, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

    def _on_main_frame_configure(self, event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self.canvas_window, width=event.width)

    def _on_mousewheel(self, event) -> None:
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        if widget is None:
            return

        current = widget
        while current is not None:
            if current == self.log_text:
                return
            parent_name = current.winfo_parent()
            if not parent_name:
                break
            current = current._nametowidget(parent_name)

        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _schedule_polls(self) -> None:
        self.root.after(STATUS_POLL_MS, self._drain_status_queue)
        self.root.after(STATUS_POLL_MS, self._drain_event_queue)
        self.root.after(PREVIEW_POLL_MS, self._refresh_preview)

    def _queue_event(self, message: str) -> None:
        self.pending_events.put(message)

    def _queue_status(self, snapshot: EngineSnapshot) -> None:
        self.pending_status.put(snapshot)

    def _drain_event_queue(self) -> None:
        while True:
            try:
                message = self.pending_events.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(STATUS_POLL_MS, self._drain_event_queue)

    def _drain_status_queue(self) -> None:
        latest = None
        while True:
            try:
                latest = self.pending_status.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            self.latest_snapshot = latest
            self._apply_snapshot(latest)
        else:
            self._apply_snapshot(self.engine.get_snapshot())
        self.root.after(STATUS_POLL_MS, self._drain_status_queue)

    def _format_channel_option(self, analog_channel_number: int) -> str:
        return f"A{analog_channel_number} / Channel {analog_channel_number}"

    def _refresh_channel_options(self) -> None:
        self.channel_option_lookup = {}

        options = ["Off"]
        for analog_channel_number in range(1, ANALOG_CHANNEL_COUNT + 1):
            label = self._format_channel_option(analog_channel_number)
            self.channel_option_lookup[label] = analog_channel_number
            options.append(label)

        combo_values = tuple(options)
        for label in CHANNEL_ORDER:
            self.mapping_combos[label]["values"] = combo_values
            current = self.mapping_vars[label].get()
            if current.isdigit():
                analog_channel_number = int(current)
                if 1 <= analog_channel_number <= ANALOG_CHANNEL_COUNT:
                    self.mapping_vars[label].set(self._format_channel_option(analog_channel_number))
                    continue

            if current == "Off":
                pending_index = self.pending_default_mapping.get(label)
                if pending_index is not None:
                    analog_channel_number = pending_index + 1
                    if 1 <= analog_channel_number <= ANALOG_CHANNEL_COUNT:
                        self.mapping_vars[label].set(self._format_channel_option(analog_channel_number))
                        continue

            if current not in options:
                self.mapping_vars[label].set("Off")

    def _apply_snapshot(self, snapshot: EngineSnapshot) -> None:
        self.status_var.set(snapshot.state)
        self.detail_var.set(snapshot.state_detail)
        self.connection_var.set("Connected" if snapshot.connected else "Disconnected")

        if snapshot.acquisition_on is None:
            self.acquisition_var.set("Unknown")
        else:
            self.acquisition_var.set("ON" if snapshot.acquisition_on else "OFF")

        self.samples_var.set(str(snapshot.samples_pushed))
        self.frames_var.set(f"{snapshot.input_frames_received} / dropped {snapshot.dropped_input_frames}")
        self.queue_var.set(str(snapshot.queue_depth))
        self.last_frame_var.set(self._format_age(snapshot.last_frame_age))
        self.last_push_var.set(self._format_age(snapshot.last_push_age))
        self.reconnect_var.set(str(snapshot.reconnect_attempts))
        self.stream_var.set(snapshot.stream_name or "--")

        self.channels_list.delete(0, "end")
        for line in snapshot.enabled_channels:
            self.channels_list.insert("end", line)

        self._refresh_channel_options()
        self._update_workflow_controls(snapshot)

    def _refresh_preview(self) -> None:
        self.preview.render(self.engine.preview_history, self.latest_snapshot.active_labels)
        self.root.after(PREVIEW_POLL_MS, self._refresh_preview)

    def _format_age(self, age: Optional[float]) -> str:
        if age is None:
            return "--"
        return f"{age:.1f}s"

    def _parse_default_mapping(self, text: str) -> Dict[str, int]:
        mapping = {}
        if not text:
            return mapping
        for part in text.replace(",", " ").split():
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip().lower()
            try:
                raw_value = value.strip().lower()
                if raw_value.startswith("a"):
                    idx = int(raw_value[1:]) - 1
                else:
                    idx = int(raw_value) - 1
            except ValueError:
                continue
            if idx < 0:
                continue
            if key == "ecg":
                mapping["ECG"] = idx
            elif key in {"rsp", "resp", "respiration"}:
                mapping["RSP"] = idx
            elif key in {"eda", "gsr"}:
                mapping["EDA"] = idx
        return mapping

    def _collect_mapping(self) -> Dict[str, int]:
        mapping = {}
        for label in CHANNEL_ORDER:
            raw = self.mapping_vars[label].get()
            if raw == "Off":
                continue
            if raw in self.channel_option_lookup:
                analog_channel_number = self.channel_option_lookup[raw]
                mapping[label] = self.engine.resolve_analog_channel_to_delivery_index(analog_channel_number)
            else:
                mapping[label] = int(raw)
        return mapping

    def _safe_collect_mapping(self) -> Tuple[Optional[Dict[str, int]], Optional[str]]:
        try:
            return self._collect_mapping(), None
        except Exception as exc:
            return None, str(exc)

    def _update_widget_state(self, widget, enabled: bool) -> None:
        widget.configure(state="normal" if enabled else "disabled")

    def _update_combobox_state(self, combo: ttk.Combobox, enabled: bool) -> None:
        combo.configure(state="readonly" if enabled else "disabled")

    def _update_workflow_controls(self, snapshot: EngineSnapshot) -> None:
        selected_mapping, mapping_error = self._safe_collect_mapping()
        has_selected_mapping = bool(selected_mapping)
        mapping_confirmed = selected_mapping == snapshot.mapping if selected_mapping is not None else False

        connected = snapshot.connected
        streaming = snapshot.streaming
        acquisition_on = snapshot.acquisition_on is True
        fault = snapshot.state == "FAULT"

        can_connect = not connected and not streaming
        can_refresh = connected and not streaming
        can_edit_mapping = connected and not streaming
        can_confirm_mapping = connected and not streaming and has_selected_mapping and mapping_error is None
        can_start_acquisition = connected and not streaming and mapping_confirmed and not acquisition_on
        can_stop_acquisition = connected and not streaming and acquisition_on
        can_start_stream = connected and not streaming and mapping_confirmed and acquisition_on
        can_stop_stream = streaming
        can_reset = connected or streaming or fault

        self._update_widget_state(self.connect_button, can_connect)
        self._update_widget_state(self.refresh_button, can_refresh)
        self._update_widget_state(self.apply_mapping_button, can_confirm_mapping)
        self._update_widget_state(self.acq_on_button, can_start_acquisition)
        self._update_widget_state(self.acq_off_button, can_stop_acquisition)
        self._update_widget_state(self.start_stream_button, can_start_stream)
        self._update_widget_state(self.stop_stream_button, can_stop_stream)
        self._update_widget_state(self.reset_button, can_reset)

        session_enabled = connected and not streaming
        self._update_widget_state(self.session_entry, session_enabled)
        for combo in self.mapping_combos.values():
            self._update_combobox_state(combo, can_edit_mapping)

        if fault:
            self.next_step_var.set("Next step: Press Reset, then reconnect to the prepared AcqKnowledge session.")
        elif streaming:
            self.next_step_var.set("Next step: Monitor the stream. Stop LSL streaming when the recording is complete.")
        elif not connected:
            self.next_step_var.set("Next step: Connect to the prepared AcqKnowledge session.")
        elif mapping_error:
            self.next_step_var.set(f"Next step: Fix the channel mapping. {mapping_error}")
        elif not has_selected_mapping:
            self.next_step_var.set("Next step: Choose the AcqKnowledge channels to use, then confirm the mapping.")
        elif not mapping_confirmed:
            self.next_step_var.set("Next step: Confirm the selected mapping so the app can validate the prepared channels.")
        elif not acquisition_on:
            self.next_step_var.set("Next step: Start acquisition in AcqKnowledge from this GUI.")
        else:
            self.next_step_var.set("Next step: Start LSL streaming.")

    def _run_async(self, action: Callable[[], None], success_message: Optional[str] = None) -> None:
        def worker() -> None:
            try:
                action()
            except Exception as exc:
                logger.exception("GUI action failed")
                self.root.after(0, lambda: messagebox.showerror("BIOPAC LSL", str(exc)))
            else:
                if success_message:
                    self.engine.log_event(success_message)

        threading.Thread(target=worker, daemon=True).start()

    def connect(self) -> None:
        self._run_async(self.engine.connect)

    def refresh_channels(self) -> None:
        self._run_async(self.engine.refresh_channels)

    def set_acquisition(self, desired_on: bool) -> None:
        self._run_async(lambda: self.engine.set_acquisition(desired_on))

    def apply_mapping(self) -> None:
        session_id = self.session_var.get().strip()
        mapping = self._collect_mapping()

        def action() -> None:
            self.engine.set_mapping(mapping, session_id)

        self._run_async(action)

    def start_streaming(self) -> None:
        snapshot = self.engine.get_snapshot()
        mapping, mapping_error = self._safe_collect_mapping()
        if mapping_error:
            messagebox.showerror("BIOPAC LSL", mapping_error)
            return
        if not mapping:
            messagebox.showerror("BIOPAC LSL", "Apply a valid mapping before starting")
            return
        if snapshot.mapping != mapping:
            messagebox.showerror("BIOPAC LSL", "Confirm the mapping before starting LSL streaming")
            return
        self._run_async(self.engine.start_streaming)

    def stop_streaming(self) -> None:
        self._run_async(self.engine.stop_streaming)

    def reset_engine(self) -> None:
        def action() -> None:
            self.engine.shutdown()

        self._run_async(action)

    def on_close(self) -> None:
        try:
            self.engine.shutdown()
        finally:
            self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = BiopacApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
BIOPAC (AcqKnowledge NDT) -> LSL streamer (ECG / RSP / EDA, flexible 1-3 channels)

Goal:
- Works if you stream only one channel (e.g., RSP only) -> LSL has 1 channel labeled "RSP"
- Works if you stream two channels -> LSL has 2 channels labeled per your mapping
- Works if you stream three channels -> LSL has 3 channels

Key idea:
- AcqKnowledge/NDT delivers frames in the order of ENABLED channels.
- You choose which enabled channel index corresponds to ECG/RSP/EDA.
- No need to rename channels in AcqKnowledge.

Requires:
- biopacndt
- pylsl
"""

import biopacndt
import os
import sys
import time
import logging
from datetime import datetime
from pylsl import StreamInfo, StreamOutlet, local_clock

# -------------------------
# User settings
# -------------------------
SRATE = 500                # set to the rate you want your LSL stream to be treated as
REST_TIME = 1.0 / SRATE
STREAM_NAME_BASE = "Biopac"
STREAM_TYPE = "PsychoPhys"
DEFAULT_MAPPING_ENV = "BIOPAC_DEFAULT_MAPPING"

# Units (edit if you want)
UNITS = {
    "ECG": "microvolts",
    "RSP": "a.u.",
    "EDA": "microsiemens",
}

aq_toggle_state = False


def get_time_vec():
    return datetime.now().strftime("%Y%m%d%H%M%S%f")


fileName = f"bp_ecg_rsp_eda_{get_time_vec()}.log"
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    filename=fileName,
)
logger = logging.getLogger(__name__)


class StreamData:
    """Callback receiver for BIOPAC frames from AcqKnowledge NDT server."""
    def __init__(self):
        self._frames = []

    def handleAcquiredData(self, hardwareIndex, frame, channelsInSlice):
        # frame is typically a tuple of floats, length = enabled channels
        self._frames.append(list(frame))

    def latest_frame(self):
        if not self._frames:
            return None
        return self._frames[-1]


def start_biopac_server():
    global acq_server, data_server, stream_data, enabled_channels

    print("Attempting to connect to AcqKnowledge...")
    logger.info("Attempting to connect to AcqKnowledge")

    acq_server = biopacndt.AcqNdtQuickConnect()
    if not acq_server:
        logger.error("Could not connect to AcqKnowledge Server")
        print("Could not connect to AcqKnowledge Server.")
        sys.exit(1)

    print("Established connection to AcqKnowledge Server.")
    logger.info("Established connection to AcqKnowledge Server")

    enabled_channels = acq_server.DeliverAllEnabledChannels()
    singleConnectPort = acq_server.getSingleConnectionModePort()

    data_server = biopacndt.AcqNdtDataServer(singleConnectPort, enabled_channels)

    stream_data = StreamData()
    data_server.RegisterCallback("OutputData", stream_data.handleAcquiredData)

    data_server.Start()
    print("Acquisition data server started... waiting 3 seconds for data...")
    logger.info("Started acquisition data server")

    time.sleep(3)


def print_enabled_channels(enabled_channels):
    print("Enabled channels reported by AcqKnowledge (this is the delivery order):")
    for i, ch in enumerate(enabled_channels):
        # ch is often a dict-like object; print it compactly
        print(f"  [{i}] {ch}")


def parse_mapping_input(text):
    """
    Accepts input like:
      "ecg=0"
      "rsp=0 ecg=1"
      "eda=2 rsp=0 ecg=1"

    Returns:
      mapping dict: {"ECG":0, "RSP":1, "EDA":2} (subset allowed)
    """
    text = text.strip()
    if not text:
        return {}

    mapping = {}
    parts = text.replace(",", " ").split()
    for p in parts:
        if "=" not in p:
            raise ValueError(f"Bad token '{p}'. Use form ecg=0 rsp=1 eda=2")
        k, v = p.split("=", 1)
        k = k.strip().lower()
        v = v.strip()

        if k in ["ecg"]:
            key = "ECG"
        elif k in ["rsp", "resp", "respiration"]:
            key = "RSP"
        elif k in ["eda", "gsr"]:
            key = "EDA"
        else:
            raise ValueError(f"Unknown key '{k}'. Use ecg, rsp/resp, eda/gsr.")

        idx = int(v)
        if idx < 0:
            raise ValueError("Index must be >= 0.")
        mapping[key] = idx

    # sanity: no duplicate indices
    if len(set(mapping.values())) != len(mapping.values()):
        raise ValueError("Two signals mapped to the same index. Each must be unique.")

    return mapping


def build_channel_order(mapping):
    """
    Return an ordered list of labels for the LSL stream.
    We keep a consistent order: ECG, RSP, EDA (only those present),
    regardless of the actual AcqKnowledge order.
    """
    order = []
    for label in ["ECG", "RSP", "EDA"]:
        if label in mapping:
            order.append(label)
    return order


def create_lsl_stream(channel_labels):
    global stream_info, stream_outlet

    n_channels = len(channel_labels)
    stream_name = STREAM_NAME_BASE + " " + "-".join(channel_labels)

    stream_info = StreamInfo(
        stream_name,
        STREAM_TYPE,
        n_channels,
        SRATE,
        "float32",
        f"biopac_{'_'.join(channel_labels).lower()}_{get_time_vec()}",
    )

    # metadata
    stream_info.desc().append_child_value("manufacturer", "Biopac")
    chns = stream_info.desc().append_child("channels")
    for label in channel_labels:
        ch = chns.append_child("channel")
        ch.append_child_value("label", label)
        ch.append_child_value("unit", UNITS.get(label, "a.u."))
        ch.append_child_value("type", label)

    stream_outlet = StreamOutlet(stream_info)
    print(f"Created LSL stream: {stream_name} ({n_channels} ch @ {SRATE} Hz)")
    logger.info(f"Created LSL stream: {stream_name} ({n_channels} ch @ {SRATE} Hz)")


def toggle_acquisition():
    global aq_toggle_state

    print("Current acquisition toggle state:", aq_toggle_state)
    toggle_request = input("Toggle acquisition? (Y/N) ").strip().lower()
    if toggle_request == "y":
        aq_toggle_state = not aq_toggle_state
        acq_server.toggleAcquisition()
        print("Toggled acquisition... waiting 2 seconds...")
        logger.info(f"Toggled acquisition; new state={aq_toggle_state}")
        time.sleep(2)
    else:
        print("Keeping current state.")


def send_stream_loop(mapping, channel_labels):
    global aq_toggle_state

    if not aq_toggle_state:
        print("Acquisition appears OFF. Toggle it ON first.")
        toggle_acquisition()

    send_request = input("Press Y to begin streaming data over LSL: ").strip().lower()
    if send_request != "y":
        print("Not streaming. Exiting.")
        return

    print("Now sending data...")
    print("Open LabRecorder, select the stream, click Start. Press Ctrl+C here to stop.")
    logger.info("Begin sending data over LSL")

    start_time = local_clock()
    sent_samples = 0
    printed_once = False

    # Precompute indices in the same order as channel_labels
    indices = [mapping[label] for label in channel_labels]

    try:
        while True:
            elapsed = local_clock() - start_time
            required = int(SRATE * elapsed) - sent_samples

            if required > 0:
                for _ in range(required):
                    frame = stream_data.latest_frame()
                    if frame is None:
                        time.sleep(REST_TIME)
                        continue

                    # Build LSL sample in desired order (ECG/RSP/EDA)
                    sample = []
                    for idx in indices:
                        if idx >= len(frame):
                            # enabled channels changed or mapping wrong
                            sample.append(float("nan"))
                        else:
                            sample.append(float(frame[idx]))

                    stream_outlet.push_sample(sample)
                    sent_samples += 1

                    if not printed_once:
                        printed_once = True
                        print(f"First pushed sample ({channel_labels}): {sample}")
                        logger.info(f"First pushed sample ({channel_labels}): {sample}")

            time.sleep(REST_TIME)

    except KeyboardInterrupt:
        tidy_up()


def tidy_up():
    print("\nStopping...")
    logger.info("Stopping streamer / cleaning up")

    try:
        data_server.Stop()
        print("Stopped data server.")
        logger.info("Stopped data server")
    except Exception as e:
        print(f"Warning: error stopping data server: {e}")
        logger.warning(f"Error stopping data server: {e}")

    try:
        if aq_toggle_state:
            acq_server.toggleAcquisition()
            print("Toggled acquisition OFF.")
            logger.info("Toggled acquisition OFF during cleanup")
            time.sleep(1)
    except Exception as e:
        print(f"Warning: error toggling acquisition during cleanup: {e}")
        logger.warning(f"Error toggling acquisition during cleanup: {e}")

    print("All finished.")


def main():
    start_biopac_server()
    print_enabled_channels(enabled_channels)

    print("\nMap which enabled channel index is which signal.")
    print("Examples:")
    print("  ecg=0")
    print("  rsp=0")
    print("  eda=0")
    print("  rsp=0 ecg=1")
    print("  ecg=0 rsp=1 eda=2")
    default_mapping = os.environ.get(DEFAULT_MAPPING_ENV, "").strip()
    if default_mapping:
        mapping_text = input(f"Enter mapping [{default_mapping}]: ").strip()
        if not mapping_text:
            mapping_text = default_mapping
    else:
        mapping_text = input("Enter mapping: ").strip()

    try:
        mapping = parse_mapping_input(mapping_text)
    except Exception as e:
        print(f"Mapping error: {e}")
        return

    if not mapping:
        print("No mapping provided. Exiting.")
        return

    # validate against enabled channel count
    max_idx = max(mapping.values())
    if max_idx >= len(enabled_channels):
        print(f"Mapping error: you used index {max_idx}, but only {len(enabled_channels)} enabled channels exist.")
        return

    channel_labels = build_channel_order(mapping)
    create_lsl_stream(channel_labels)

    toggle_acquisition()
    send_stream_loop(mapping, channel_labels)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
BIOPAC (AcqKnowledge NDT) -> LSL streamer (single-channel ECG version)

What this fixes vs your original:
1) Stream channel-count matches what you actually send (ECG only = 1 channel)
2) Correct sample pacing counter (sent_samples += 1)
3) Safer handling when no samples have arrived yet
4) Helpful logging + prints so you can see it is truly pushing samples

Requirements:
- biopacndt
- pylsl

Usage:
- Start AcqKnowledge and make sure ECG channel is enabled
- Run this script
- Toggle acquisition ON when prompted
- Start streaming when prompted
- In LabRecorder: select the stream and click Start
"""

import biopacndt
import sys
import time
import logging
from datetime import datetime
from pylsl import StreamInfo, StreamOutlet, local_clock


# -------------------------
# User settings
# -------------------------
SRATE = 500                # must match your intended streaming rate
REST_TIME = 1.0 / SRATE
STREAM_NAME = "Biopac ECG"
STREAM_TYPE = "PsychoPhys"
CHANNEL_NAMES = ["ECG"]    # single channel


aq_toggle_state = False


def get_time_vec():
    right_now = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return int(right_now)


fileName = "bp_ecg_" + str(get_time_vec()) + ".log"
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    filename=fileName,
)
logger = logging.getLogger(__name__)


class StreamData:
    """
    Callback receiver for BIOPAC frames from AcqKnowledge NDT server.
    Stores frames; we stream out the most recent sample.
    """
    def __init__(self):
        self._chanData = []

    def handleAcquiredData(self, hardwareIndex, frame, channelsInSlice):
        # frame is typically a tuple of floats, length = enabled channels
        self._chanData.append(list(frame))

    def latest_sample(self):
        if not self._chanData:
            return None
        return self._chanData[-1]


def start_biopac_server():
    global acq_server, data_server, stream_data

    print("Attempting to connect to AcqKnowledge...")
    logger.info("Attempting to connect to AcqKnowledge")

    acq_server = biopacndt.AcqNdtQuickConnect()
    if not acq_server:
        logger.error("Could not connect to AcqKnowledge Server")
        print("Could not connect to AcqKnowledge Server.")
        sys.exit(1)

    logger.info("Established connection to AcqKnowledge Server")
    print("Established connection to AcqKnowledge Server.")

    enabledChannels = acq_server.DeliverAllEnabledChannels()
    singleConnectPort = acq_server.getSingleConnectionModePort()

    data_server = biopacndt.AcqNdtDataServer(singleConnectPort, enabledChannels)

    stream_data = StreamData()
    data_server.RegisterCallback("OutputData", stream_data.handleAcquiredData)

    data_server.Start()
    logger.info("Started acquisition data server")
    print("Acquisition data server started... waiting 5 seconds for data...")

    # small delay so callbacks begin populating
    time.sleep(5)

    # Optional: print how many channels AcqKnowledge says are enabled
    try:
        enabledChannels = acq_server.DeliverAllEnabledChannels()
        print(f"AcqKnowledge enabled channels reported: {len(enabledChannels)}")
        logger.info(f"AcqKnowledge enabled channels reported: {len(enabledChannels)}")
    except Exception as e:
        logger.warning(f"Could not re-check enabled channels: {e}")


def create_lsl_stream():
    global stream_info, stream_outlet

    n_channels = len(CHANNEL_NAMES)
    stream_info = StreamInfo(STREAM_NAME, STREAM_TYPE, n_channels, SRATE, "float32", "biopac_ecg_uid")

    # metadata
    stream_info.desc().append_child_value("manufacturer", "Biopac")
    chns = stream_info.desc().append_child("channels")
    for label in CHANNEL_NAMES:
        ch = chns.append_child("channel")
        ch.append_child_value("label", label)
        ch.append_child_value("unit", "microvolts")
        ch.append_child_value("type", label)

    stream_outlet = StreamOutlet(stream_info)
    logger.info(f"Created LSL stream: {STREAM_NAME} ({n_channels} ch @ {SRATE} Hz)")
    print(f"Created LSL stream: {STREAM_NAME} ({n_channels} ch @ {SRATE} Hz)")


def toggle_acquisition():
    global aq_toggle_state

    print("Current acquisition toggle state:", aq_toggle_state)
    toggle_request = input("Do you want to toggle acquisition? (Y/N) ").strip().lower()
    if toggle_request == "y":
        aq_toggle_state = not aq_toggle_state
        acq_server.toggleAcquisition()
        logger.info(f"Toggled acquisition; new state={aq_toggle_state}")
        print("Toggled Biopac acquisition... waiting 2 seconds...")
        time.sleep(2)
    else:
        print("Keeping the current state.")


def send_stream_loop():
    global aq_toggle_state

    if not aq_toggle_state:
        print("Acquisition appears OFF. Let's toggle it ON first.")
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

    try:
        while True:
            elapsed_time = local_clock() - start_time
            required_samples = int(SRATE * elapsed_time) - sent_samples

            if required_samples > 0:
                for _ in range(required_samples):
                    if aq_toggle_state:
                        latest = stream_data.latest_sample()
                        if latest is None:
                            # no BIOPAC data yet
                            continue

                        # Expect ECG-only to be 1 value, but BIOPAC may deliver more channels.
                        # We take the first value as ECG by default.
                        ecg_value = float(latest[0])
                        mysample = [ecg_value]
                    else:
                        mysample = [float("nan")]

                    # Push sample (length must equal n_channels = 1)
                    stream_outlet.push_sample(mysample)
                    sent_samples += 1  # IMPORTANT FIX

                    # Print once after we successfully send at least one sample
                    if not printed_once:
                        printed_once = True
                        print(f"First sample pushed: {mysample}")
                        logger.info(f"First sample pushed: {mysample}")

            time.sleep(REST_TIME)

    except KeyboardInterrupt:
        tidy_up()


def tidy_up():
    print("\nStopping...")
    logger.info("Stopping streamer / cleaning up")

    # Stop data server
    try:
        data_server.Stop()
        logger.info("Stopped data server")
        print("Stopped data server.")
    except Exception as e:
        logger.warning(f"Error stopping data server: {e}")
        print(f"Warning: error stopping data server: {e}")

    # Turn off acquisition if currently on
    try:
        if aq_toggle_state:
            acq_server.toggleAcquisition()
            logger.info("Toggled acquisition OFF during cleanup")
            print("Toggled acquisition OFF.")
            time.sleep(1)
    except Exception as e:
        logger.warning(f"Error toggling acquisition during cleanup: {e}")
        print(f"Warning: error toggling acquisition during cleanup: {e}")

    # Delete objects
    try:
        del globals()["data_server"]
    except Exception:
        pass
    try:
        del globals()["stream_data"]
    except Exception:
        pass
    try:
        del globals()["acq_server"]
    except Exception:
        pass

    print("All finished.")


def main():
    start_biopac_server()
    create_lsl_stream()
    toggle_acquisition()
    send_stream_loop()


if __name__ == "__main__":
    main()

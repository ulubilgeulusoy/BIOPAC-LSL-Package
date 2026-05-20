# BIOPAC LSL GUI Streamer

This repository contains a single GUI application for streaming physiological data from BIOPAC AcqKnowledge over Lab Streaming Layer (LSL):

- `Biopac_ECG_RSP_EDA_LSL.py`

The app is designed for an operator workflow where BIOPAC / AcqKnowledge is configured first, signal quality is checked there, and this GUI is then used to attach to that prepared session, start acquisition if needed, and publish ECG / respiration (RSP) / electrodermal activity (EDA) over LSL.

## Files
- `Biopac_ECG_RSP_EDA_LSL.py`: main GUI streamer
- `biopacndt.py`: BIOPAC NDT Python module used by the app (not included in the repository)

## Requirements
- Python 3.8+
- BIOPAC AcqKnowledge with Network Data Transfer (NDT) enabled
- A valid BIOPAC NDT license
- `pylsl`
- LabRecorder or another LSL client if you want to record the stream

Install the Python dependency with:

```bash
pip install pylsl
```

## NDT License Check
If you are not sure whether your AcqKnowledge installation has NDT available:

1. Open AcqKnowledge.
2. Go to `Display > Preferences > Networking`.
3. Look for `Enable network data transfer`.
4. If that option is present and usable, NDT is available.
5. If it is missing or disabled, this application will not work until NDT is licensed and enabled.

Notes:
- NDT is a separate BIOPAC feature.
- In many setups, the license is tied to the BIOPAC USB dongle.

## Recommended Workflow
This GUI assumes AcqKnowledge is already prepared before you use it:

1. Set up the BIOPAC channels and hardware in AcqKnowledge.
2. Verify the signals look correct in AcqKnowledge.
3. Stop acquisition after your setup check if needed.
4. Launch this GUI.
5. Connect to the prepared AcqKnowledge session.
6. Confirm which AcqKnowledge analog channels should be used for RSP / ECG / EDA.
7. Start acquisition from the GUI if AcqKnowledge is not already acquiring.
8. Start LSL streaming.

## Running The App
Start the GUI with:

```bash
python Biopac_ECG_RSP_EDA_LSL.py
```

## GUI Workflow
The GUI is step-guided and enables controls in order.

1. `Connect`
   Attaches to the running AcqKnowledge session and reads the enabled NDT channels.
2. `Confirm Mapping`
   Assigns `RSP`, `ECG`, and `EDA` to AcqKnowledge analog channels such as `A1 / Channel 1`.
   Use `Off` for signals you are not streaming.
3. `Start Acquisition`
   Tells AcqKnowledge to begin live acquisition if it is not already running.
4. `Start LSL Streaming`
   Creates the LSL outlet and begins forwarding the incoming BIOPAC data.
5. `Stop LSL Streaming`
   Stops the LSL stream while leaving the GUI connected.
6. `Reset`
   Clears the current session state and disconnects the app.

## Mapping Behavior
The mapping UI is based on AcqKnowledge analog channel names, not raw internal delivery indices.

Examples:
- `A1 / Channel 1`
- `A9 / Channel 9`
- `A13 / Channel 13`

Internally, the app resolves those channel selections to the currently active NDT delivery order. If you choose a channel that is not actually enabled in AcqKnowledge, the GUI will show an error instead of silently using the wrong stream.

## Monitoring Features
The GUI includes:
- live connection and acquisition state
- guided next-step instructions
- rolling event log
- bounded buffering between BIOPAC callbacks and LSL sending
- stale-data watchdog monitoring
- reconnect attempts if BIOPAC delivery stalls
- rolling signal preview for quick operator checks

## Logs
The app writes timestamped log files next to the script using names like:

```text
bp_ecg_rsp_eda_20260423142712462822.log
```

These logs include state changes, mapping confirmation, streaming events, warnings, and reconnect attempts.

## Limitations
- The signal preview is intended for operator monitoring, not clinical interpretation.
- If the enabled channels inside AcqKnowledge change, you should refresh channels and re-confirm mapping before starting a new run.
- This project leaves `biopacndt.py` unchanged and builds robustness in the GUI application around it.

## Acknowledgment

This project was developed independently, with high-level workflow inspiration from Greg Bales' `Trust_LSL` repository for BIOPAC-to-LSL streaming concepts:
https://github.com/greg1877/Trust_LSL

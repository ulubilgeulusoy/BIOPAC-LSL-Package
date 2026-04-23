# Biopac LSL Streamers

Python helpers to pull physiological data from Biopac AcqKnowledge (NDT) and publish them as Lab Streaming Layer (LSL) streams. Two scripts are included:
- `Biopac_ECG_LSL.py`: single-channel ECG streamer.
- `Biopac_ECG_RSP_EDA_LSL.py`: flexible 1-3 channel streamer for ECG, respiration (RSP), and electrodermal activity (EDA); works for any subset of those signals and now includes a GUI for setup and live monitoring.

## Requirements
- Python 3.8+
- Biopac AcqKnowledge with the NDT server enabled
- A BIOPAC Network Data Transfer (NDT) license: https://www.biopac.com/product/network-data-transfer-licenses/
- `biopacndt.py` from your licensed BIOPAC NDT installation/package; this file is not included in this repo
- `pylsl` (install via `pip install pylsl`)
- LabRecorder or another LSL receiver to capture the stream

## Setup
1) Install the external dependency:
   ```
   pip install pylsl
   ```
2) Obtain a BIOPAC NDT license and the accompanying `biopacndt.py` file from BIOPAC:
   https://www.biopac.com/product/network-data-transfer-licenses/
3) Keep `biopacndt.py` alongside the scripts so it can be imported. This repository does not ship that file.
4) Start AcqKnowledge, enable the channels you want, and ensure the NDT server is available.
5) (Optional) Verify firewall settings allow localhost communication for NDT/LSL.

## NDT License Check
If you are unsure whether your AcqKnowledge installation has a BIOPAC Network Data Transfer (NDT) license, check directly in AcqKnowledge before troubleshooting these scripts.

1) Open AcqKnowledge.
2) Go to `Display > Preferences > Networking`.
3) Look for the `Enable network data transfer` option.
4) If that option is present and clickable, NDT is available.
5) If the option is grayed out or missing, you likely do not have an active NDT license available to AcqKnowledge and this repository will not work for you.

Disclaimer:
- NDT is a separately licensed BIOPAC feature.
- In many setups, the license is tied to the blue BIOPAC USB.
- This repository does not provide the NDT license or the `biopacndt.py` file; you must obtain both through BIOPAC.

## Usage: Single-Channel ECG (`Biopac_ECG_LSL.py`)
1) Run:
   ```
   python Biopac_ECG_LSL.py
   ```
2) Follow the prompts:
   - Connects to AcqKnowledge and starts the NDT data server.
   - Optionally toggles acquisition.
   - Press `Y` when ready to begin streaming over LSL.
3) In LabRecorder (or another LSL client), select the `Biopac ECG` stream and start recording.
4) Press `Ctrl+C` to stop; cleanup will stop the data server and toggle acquisition off if needed.

Notes for this script:
- `SRATE` and `CHANNEL_NAMES` are defined near the top of `Biopac_ECG_LSL.py` if you need to adjust them.
- Log files are timestamped (e.g., `bp_ecg_*.log`) and written next to the script.

## Usage: ECG/RSP/EDA (`Biopac_ECG_RSP_EDA_LSL.py`)
1) Run:
   ```
   python Biopac_ECG_RSP_EDA_LSL.py
   ```
   Or double-click `run_Biopac_ECG_RSP_EDA_LSL.bat`.
2) In the GUI:
   - Click `Connect` to connect to AcqKnowledge and populate the detected enabled channels.
   - Map the enabled channel indices to ECG / RSP / EDA using the dropdowns.
   - Optionally enter a session ID, which is appended to the LSL stream name.
   - Use `Acq On` / `Acq Off` to control acquisition without terminal prompts.
3) Click `Start Streaming` to create the LSL outlet and begin forwarding BIOPAC data.
4) Use the live status area, event log, and signal preview to confirm the stream is healthy during collection.
5) Click `Stop Streaming` when finished. `Reset` disconnects and clears the current session state.

Notes for this script:
- Logs are written with names like `bp_ecg_rsp_eda_*.log`.
- If the enabled channels change mid-run, unmapped indices will push `nan` values; keep your mapping aligned with the enabled-channel order.
- The GUI adds bounded buffering, stale-data monitoring, reconnect attempts, and a rolling event log to make long recordings easier to supervise.

## Attribution & Disclaimer
These scripts were generated with Codex and adapted from `Biopac_MultiChannel_LSL.py` in Greg Bales' repository: https://github.com/greg1877/Trust_LSL. Please credit that source if you share or modify this code.

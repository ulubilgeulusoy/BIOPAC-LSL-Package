# Biopac LSL Streamers

Python helpers to pull physiological data from Biopac AcqKnowledge (NDT) and publish them as Lab Streaming Layer (LSL) streams. Two scripts are included:
- `Biopac_ECG_LSL.py`: single-channel ECG streamer.
- `Biopac_ECG_RSP_EDA_LSL.py`: flexible 1–3 channel streamer for ECG, respiration (RSP), and electrodermal activity (EDA); works for any subset of those signals.

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
2) The script prints the enabled channels (in the order AcqKnowledge delivers them). Provide a mapping that states which enabled channel index corresponds to each signal:
   - Examples: If you are only using one sensor (`ecg=0`,`rsp=0`,`eda=0`). If you are using multiple (`rsp=0 ecg=1`, `ecg=0 rsp=1 eda=2`); make sure that this mapping matches the order in the Acqknowledge software.
3) The script creates an LSL stream named like `Biopac ECG-RSP-EDA` with the specified channels at the configured sample rate (`SRATE` near the top of the file; defaults to 500 Hz).
4) Press `Y` toggle acquisition when prompted, then press `Y` to start streaming. Use LabRecorder (or another LSL client) to record the stream.
5) Press `Ctrl+C` to stop; the script stops the data server and toggles acquisition off if it was turned on.

Notes for this script:
- Logs are written with names like `bp_ecg_rsp_eda_*.log`.
- If the enabled channels change mid-run, unmapped indices will push `nan` values; keep your mapping aligned with the enabled-channel order.

## Attribution & Disclaimer
These scripts were generated with Codex and adapted from `Biopac_MultiChannel_LSL.py` in Greg Bales' repository: https://github.com/greg1877/Trust_LSL. Please credit that source if you share or modify this code.

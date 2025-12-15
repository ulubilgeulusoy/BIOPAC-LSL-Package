# Biopac ECG LSL Streamer

Single-channel ECG streamer that pulls data from Biopac AcqKnowledge (NDT) and publishes it as an LSL stream. The script targets one ECG channel and keeps stream pacing aligned to the configured sample rate.

## Requirements
- Python 3.8+
- Biopac AcqKnowledge with NDT server enabled
- Packages in `requirements.txt` (`biopacndt`, `pylsl`)
- LabRecorder or another LSL receiver to record the stream

## Setup
1) Install Python dependencies:
   ```
   pip install -r requirements.txt
   ```
2) Start AcqKnowledge, ensure your ECG channel is enabled, and NDT is available.
3) (Optional) Verify firewall settings allow localhost communication for NDT/LSL.

## Usage
1) Run the script:
   ```
   python Biopac_ECG_LSL.py
   ```
2) Follow the prompts:
   - Connects to AcqKnowledge and starts the NDT data server.
   - Optionally toggles acquisition.
   - Press `Y` when ready to begin streaming over LSL.
3) In LabRecorder (or another LSL client), select the `Biopac ECG` stream and start recording.
4) Press `Ctrl+C` in the terminal to stop; cleanup will stop the data server and toggle acquisition off if needed.

## Notes
- Log files are written alongside the script with a timestamped name (e.g., `bp_ecg_*.log`).
- The script assumes a single ECG value per frame and sends the first channel value as ECG.
- `SRATE` and `CHANNEL_NAMES` are set near the top of `Biopac_ECG_LSL.py` if you need to adjust them.

## Attribution & Disclaimer
This script was generated with ChatGPT 5.2 and adapted from `Biopac_MultiChannel_LSL.py` in Greg Bales' repository: https://github.com/greg1877/Trust_LSL. Please credit that source if you share or modify this code.

@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_EXE=C:\Users\ulul8626\AppData\Local\miniconda3\envs\lsl_env\python.exe"
set "SCRIPT_PATH=%~dp0Biopac_ECG_RSP_EDA_LSL.py"
set "BIOPAC_DEFAULT_MAPPING=rsp=0 ecg=1"

if not exist "%PYTHON_EXE%" (
    echo Could not find lsl_env Python at:
    echo %PYTHON_EXE%
    pause
    exit /b 1
)

if not exist "%SCRIPT_PATH%" (
    echo Could not find script:
    echo %SCRIPT_PATH%
    pause
    exit /b 1
)

"%PYTHON_EXE%" "%SCRIPT_PATH%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Script exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%

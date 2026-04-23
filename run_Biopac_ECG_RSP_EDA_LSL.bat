@echo off
setlocal

cd /d "%~dp0"

set "SCRIPT_PATH=%~dp0Biopac_ECG_RSP_EDA_LSL.py"

if not exist "%SCRIPT_PATH%" (
    echo Could not find script:
    echo %SCRIPT_PATH%
    pause
    exit /b 1
)

set "PYTHON_CMD="

where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
)

if not defined PYTHON_CMD (
    where python >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_CMD=python"
    )
)

if not defined PYTHON_CMD (
    echo Could not find a Python launcher.
    echo Install Python or update this batch file with your preferred interpreter.
    pause
    exit /b 1
)

%PYTHON_CMD% "%SCRIPT_PATH%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Script exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%

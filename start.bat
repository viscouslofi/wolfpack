@echo off
rem WolfPack one-click start — double-click this file.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python 3.11+ was not found.
    echo Install it from https://www.python.org/downloads/ and check "Add python to PATH".
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python -c "import sys; print(sys.version_info >= (3, 11))"') do set PYOK=%%v
if not "%PYOK%"=="True" (
    echo WolfPack needs Python 3.11 or newer.
    python --version
    echo Get it from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Starting WolfPack on http://localhost:7100/ ...
start "" http://localhost:7100/
python server.py --port 7100
pause

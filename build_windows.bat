@echo off
REM Baut HopGrid-Host.exe und HopGrid-Client.exe nach dist\ (je eine einzelne Datei).
REM Voraussetzung: Python 3.10+ von python.org (mit "Add to PATH").
setlocal
cd /d "%~dp0"

if not exist .venv (
    python -m venv .venv || goto :error
)
.venv\Scripts\python -m pip install -q --upgrade pip || goto :error
.venv\Scripts\python -m pip install -q -r requirements.txt pyinstaller || goto :error

.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed --icon assets\icon.ico ^
  --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 ^
  --hidden-import pynput._util.win32 --hidden-import pynput._util.win32_vks ^
  --name HopGrid-Host host.py || goto :error

.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed --icon assets\icon.ico ^
  --name HopGrid-Client client.py || goto :error

echo Fertig: dist\HopGrid-Host.exe und dist\HopGrid-Client.exe
exit /b 0

:error
echo Build fehlgeschlagen.
exit /b 1

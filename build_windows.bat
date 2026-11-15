@echo off
REM Baut dist\HopGrid.exe (Host und Client in einer Datei).
REM Voraussetzung: Python 3.10+ von python.org (mit "Add to PATH").
setlocal
cd /d "%~dp0"

if not exist .venv (
    python -m venv .venv || goto :error
)
.venv\Scripts\python -m pip install -q --upgrade pip || goto :error
.venv\Scripts\python -m pip install -q -r requirements.txt pyinstaller || goto :error

.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed --icon assets\icon.ico ^
  --hidden-import host --hidden-import client --hidden-import keyring.backends.Windows ^
  --hidden-import pystray._win32 --hidden-import pyperclip --add-data assets\icon.png;assets ^
  --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 ^
  --hidden-import pynput._util.win32 --hidden-import pynput._util.win32_vks ^
  --name HopGrid hopgrid.py || goto :error
del /q HopGrid.spec 2>nul

echo Fertig: dist\HopGrid.exe
exit /b 0

:error
echo Build fehlgeschlagen.
exit /b 1

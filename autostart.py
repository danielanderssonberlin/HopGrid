"""
HopGrid – Autostart bei der Anmeldung und Speicherort für Einstellungen.

Windows: Eintrag unter HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
macOS:   LaunchAgent ~/Library/LaunchAgents/com.kenomedia.hopgrid.plist

Beides startet HopGrid bei der Anmeldung des Benutzers (nicht vor dem Login).
"""

import json
import os
import plistlib
import sys

APP_NAME = "HopGrid"
AUTOSTART_ARG = "--autostart"
MAC_LABEL = "com.kenomedia.hopgrid"


def app_data_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


def load_json(name):
    try:
        with open(os.path.join(app_data_dir(), name), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json(name, data, private=False):
    """Speichert Einstellungen. private=True: nur für den eigenen Benutzer lesbar."""
    folder = app_data_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    if private and sys.platform != "win32":
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    else:
        # Unter Windows ist %APPDATA% bereits nur für den Benutzer zugänglich
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)


def delete_json(name):
    try:
        os.remove(os.path.join(app_data_dir(), name))
    except OSError:
        pass


def launch_command():
    """Befehl, mit dem sich HopGrid selbst im Autostart-Modus startet."""
    if getattr(sys, "frozen", False):  # gebaute App/.exe
        return [sys.executable, AUTOSTART_ARG]
    script = os.path.abspath(os.path.join(os.path.dirname(__file__), "hopgrid.py"))
    return [sys.executable, script, AUTOSTART_ARG]


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _win_enable():
    import subprocess
    import winreg
    command = subprocess.list2cmdline(launch_command())
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)


def _win_disable():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
    except FileNotFoundError:
        pass


def _win_is_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------
def _mac_plist_path():
    return os.path.expanduser(f"~/Library/LaunchAgents/{MAC_LABEL}.plist")


def _mac_enable():
    path = _mac_plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump({
            "Label": MAC_LABEL,
            "ProgramArguments": launch_command(),
            "RunAtLoad": True,
            "ProcessType": "Interactive",
        }, fh)


def _mac_disable():
    try:
        os.remove(_mac_plist_path())
    except FileNotFoundError:
        pass


def _mac_is_enabled():
    return os.path.exists(_mac_plist_path())


# ---------------------------------------------------------------------------
# Öffentliche Funktionen
# ---------------------------------------------------------------------------
def supported():
    return sys.platform in ("win32", "darwin")


def enable():
    if sys.platform == "win32":
        _win_enable()
    elif sys.platform == "darwin":
        _mac_enable()


def disable():
    if sys.platform == "win32":
        _win_disable()
    elif sys.platform == "darwin":
        _mac_disable()


def is_enabled():
    if sys.platform == "win32":
        return _win_is_enabled()
    if sys.platform == "darwin":
        return _mac_is_enabled()
    return False

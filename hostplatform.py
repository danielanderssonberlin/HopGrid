"""
HopGrid Host – betriebssystemnahe Abfragen.

    CursorShape   Form des aktuellen Mauszeigers (Pfeil, Textcursor, Größe ändern, …)
    folder_at     Welcher Ordner liegt unter einer Bildschirmposition? (Schreibtisch,
                  Finder- bzw. Explorer-Fenster) – sonst Grund, warum dort nichts hin darf.

Formnamen (gemeinsam mit dem Client): arrow, ibeam, hand, resize_h, resize_v,
resize_nwse, resize_nesw, move, wait, busy, cross, no, hidden.
"""

import hashlib
import os
import subprocess
import sys

NOT_HERE = ("Dort kann nichts abgelegt werden. Zeiger auf den Schreibtisch oder in ein "
            "Ordnerfenster ({fm}) setzen und erneut loslassen.")


def _fm():
    return "Finder" if sys.platform == "darwin" else "Explorer"


# ---------------------------------------------------------------------------
# Zeigerform
# ---------------------------------------------------------------------------
class CursorShape:
    def __init__(self):
        self._known = {}
        self._mac_cache = (None, "arrow")
        try:
            if sys.platform == "win32":
                self._init_windows()
            elif sys.platform == "darwin":
                self._init_mac()
        except Exception:
            self._known = {}

    # --- Windows: Handles der Standard-Zeiger vergleichen ---
    def _init_windows(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.LoadCursorW.restype = wintypes.HANDLE
        user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
        ids = {32512: "arrow", 32513: "ibeam", 32514: "wait", 32515: "cross",
               32642: "resize_nwse", 32643: "resize_nesw", 32644: "resize_h", 32645: "resize_v",
               32646: "move", 32648: "no", 32649: "hand", 32650: "busy"}
        for cid, name in ids.items():
            handle = user32.LoadCursorW(None, cid)
            if handle:
                self._known[handle] = name

        class CURSORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]
        self._info_type = CURSORINFO
        self._user32 = user32

    def _current_windows(self):
        import ctypes
        info = self._info_type()
        info.cbSize = ctypes.sizeof(info)
        if not self._user32.GetCursorInfo(ctypes.byref(info)):
            return "arrow"
        if not (info.flags & 1):  # CURSOR_SHOWING
            return "hidden"
        return self._known.get(info.hCursor, "arrow")

    # --- macOS: Bildinhalt des Systemzeigers mit bekannten Zeigern vergleichen ---
    def _init_mac(self):
        from AppKit import NSApplication, NSCursor
        NSApplication.sharedApplication()
        candidates = {
            "arrowCursor": "arrow", "IBeamCursor": "ibeam", "IBeamCursorForVerticalLayout": "ibeam",
            "pointingHandCursor": "hand", "openHandCursor": "move", "closedHandCursor": "move",
            "resizeLeftRightCursor": "resize_h", "resizeLeftCursor": "resize_h",
            "resizeRightCursor": "resize_h", "resizeUpDownCursor": "resize_v",
            "resizeUpCursor": "resize_v", "resizeDownCursor": "resize_v",
            "crosshairCursor": "cross", "operationNotAllowedCursor": "no",
            "dragCopyCursor": "arrow", "dragLinkCursor": "arrow", "contextualMenuCursor": "arrow",
            "_windowResizeNorthWestSouthEastCursor": "resize_nwse",
            "_windowResizeNorthEastSouthWestCursor": "resize_nesw",
            "_windowResizeEastWestCursor": "resize_h", "_windowResizeNorthSouthCursor": "resize_v",
            "_moveCursor": "move", "busyButClickableCursor": "busy", "_waitCursor": "wait",
        }
        for selector, name in candidates.items():
            try:
                cursor = getattr(NSCursor, selector)()
                if cursor is not None:
                    self._known[self._mac_key(cursor)] = name
            except Exception:
                continue
        # macOS 15+: Zeiger zum Ziehen von Fensterrändern (öffentliche API)
        try:
            for position, name in ((1, "resize_v"), (2, "resize_h"), (4, "resize_v"),
                                   (8, "resize_h"), (1 | 8, "resize_nwse"), (4 | 2, "resize_nwse"),
                                   (1 | 2, "resize_nesw"), (4 | 8, "resize_nesw")):
                for directions in (1, 2, 3):
                    cursor = NSCursor.frameResizeCursorFromPosition_inDirections_(position,
                                                                                  directions)
                    if cursor is not None:
                        self._known.setdefault(self._mac_key(cursor), name)
        except Exception:
            pass

    @staticmethod
    def _mac_key(cursor):
        return hashlib.md5(bytes(cursor.image().TIFFRepresentation())).hexdigest()

    def _current_mac(self):
        from AppKit import NSCursor
        cursor = NSCursor.currentSystemCursor()
        if cursor is None:
            return "arrow"
        key = self._mac_key(cursor)
        if key == self._mac_cache[0]:
            return self._mac_cache[1]
        name = self._known.get(key, "arrow")
        self._mac_cache = (key, name)
        return name

    def current(self):
        try:
            if sys.platform == "win32" and self._known:
                return self._current_windows()
            if sys.platform == "darwin" and self._known:
                return self._current_mac()
        except Exception:
            pass
        return "arrow"


class MainThreadState:
    """macOS: AppKit- und Eingabe-Systemfunktionen dürfen nur im Hauptthread laufen,
    sonst beendet macOS die App (SIGTRAP). Im Fenster-Modus fragt der Hauptthread
    Zeigerform, kopierte Dateien und die Bedienungshilfen-Freigabe regelmäßig ab;
    die Verbindungs-Threads lesen nur die Ergebnisse."""

    def __init__(self):
        self.active = False
        self.shape = "arrow"
        self.files = []
        self.ax_trusted = True
        self.screen_ok = True
        self._shapes = None

    def start(self, root, interval_ms=100):
        if sys.platform != "darwin" or self.active:
            return
        self._shapes = CursorShape()
        self.active = True
        self._tick(root, interval_ms, 0)

    def _tick(self, root, interval_ms, n):
        try:
            self.shape = self._shapes.current()
            if n % 7 == 0:  # seltener: Zwischenablage und Freigabe
                from filetransfer import clipboard_files
                self.files = clipboard_files()
                try:
                    from ApplicationServices import AXIsProcessTrusted
                    self.ax_trusted = bool(AXIsProcessTrusted())
                except ImportError:
                    pass
                try:
                    import Quartz
                    self.screen_ok = bool(Quartz.CGPreflightScreenCaptureAccess())
                except (ImportError, AttributeError):
                    pass
        except Exception:
            pass
        try:
            root.after(interval_ms, self._tick, root, interval_ms, n + 1)
        except Exception:
            self.active = False


main_state = MainThreadState()

# Direkt zur passenden Seite der Systemeinstellungen
SETTINGS_URLS = {
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "automation": "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
}


def app_location_problem():
    """macOS: Läuft die App nicht aus /Applications (oder aus einem versteckten
    Quarantäne-Ordner), merkt sich macOS die Rechte oft nicht zuverlässig."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    exe = os.path.realpath(sys.executable)
    app = exe.split("/Contents/MacOS/")[0]
    if "/AppTranslocation/" in exe or not app.startswith("/Applications/"):
        return app
    return None


def reveal_app():
    app = os.path.realpath(sys.executable).split("/Contents/MacOS/")[0]
    subprocess.Popen(["open", "-R", app])


def open_settings(kind):
    if sys.platform == "darwin" and kind in SETTINGS_URLS:
        subprocess.Popen(["open", SETTINGS_URLS[kind]])


# ---------------------------------------------------------------------------
# Ordner unter dem Zeiger
# ---------------------------------------------------------------------------
def folder_at(x, y):
    """Liefert (ordner, anzeigename, fehler). Genau einer von ordner/fehler ist gesetzt.
    x, y: Bildschirmkoordinaten wie für die Maus (macOS Punkte, Windows Pixel)."""
    try:
        if sys.platform == "darwin":
            return _mac_folder_at(x, y)
        if sys.platform == "win32":
            return _win_folder_at(x, y)
    except Exception as exc:
        return None, "", f"Ziel konnte nicht ermittelt werden: {exc}"
    return None, "", "Ablegen am Zeiger wird auf diesem System nicht unterstützt."


def _desktop_mac():
    return os.path.join(os.path.expanduser("~"), "Desktop")


def _mac_folder_at(x, y):
    import Quartz
    from AppKit import NSRunningApplication
    options = (Quartz.kCGWindowListOptionOnScreenOnly
               | Quartz.kCGWindowListExcludeDesktopElements)
    windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    own_pid = os.getpid()
    for w in windows:  # von vorne nach hinten
        bounds = w.get("kCGWindowBounds") or {}
        bx, by = bounds.get("X", 0), bounds.get("Y", 0)
        bw, bh = bounds.get("Width", 0), bounds.get("Height", 0)
        if not (bx <= x < bx + bw and by <= y < by + bh):
            continue
        if w.get("kCGWindowAlpha", 1) == 0 or w.get("kCGWindowOwnerPID") == own_pid:
            continue
        layer = w.get("kCGWindowLayer", 0)
        if layer < 0:
            continue
        if layer > 0:
            return None, "", NOT_HERE.format(fm=_fm())  # Dock, Menüleiste, Overlays
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(
            w.get("kCGWindowOwnerPID"))
        if app is None or app.bundleIdentifier() != "com.apple.finder":
            return None, "", NOT_HERE.format(fm=_fm())
        return _finder_window_folder(bx, by, bw, bh)
    return _desktop_mac(), "Schreibtisch", None


_FINDER_SCRIPT = '''
tell application "Finder"
    set out to ""
    repeat with w in (every Finder window)
        try
            set b to bounds of w
            set p to POSIX path of (target of w as alias)
            set out to out & (item 1 of b) & "," & (item 2 of b) & "," & (item 3 of b) & "," & (item 4 of b) & tab & p & linefeed
        end try
    end repeat
    return out
end tell
'''


def _finder_window_folder(bx, by, bw, bh):
    """Pfad des Finder-Fensters mit diesen Maßen (per AppleScript; macOS fragt einmal
    nach der Erlaubnis, den Finder zu steuern)."""
    result = subprocess.run(["osascript", "-e", _FINDER_SCRIPT], capture_output=True,
                            text=True, timeout=8)
    if result.returncode != 0:
        if "-1743" in result.stderr or "not allowed" in result.stderr.lower():
            return None, "", ("HopGrid darf den Finder nicht abfragen. Auf dem anderen Rechner "
                              "unter Systemeinstellungen › Datenschutz & Sicherheit › "
                              "Automation HopGrid › Finder erlauben.")
        return None, "", "Der Ordner des Finder-Fensters konnte nicht ermittelt werden."
    best, best_diff = None, None
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        coords, path = line.split("\t", 1)
        try:
            left, top, right, bottom = (float(v) for v in coords.split(","))
        except ValueError:
            continue
        diff = abs(left - bx) + abs(top - by) + abs(right - (bx + bw)) + abs(bottom - (by + bh))
        if best_diff is None or diff < best_diff:
            best, best_diff = path, diff
    if best is None or best_diff > 120:
        return None, "", ("Dieses Finder-Fenster zeigt keinen normalen Ordner "
                          "(z. B. „Zuletzt benutzt“).")
    best = best.rstrip("/") or "/"
    return best, os.path.basename(best) or best, None


def _desktop_windows():
    import ctypes
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0:  # CSIDL_DESKTOPDIRECTORY
        return buf.value
    return os.path.join(os.path.expanduser("~"), "Desktop")


_PS_SCRIPT = r'''
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$shell = New-Object -ComObject Shell.Application
foreach ($w in $shell.Windows()) {
    try {
        if ($w.HWND -eq {hwnd}) {
            $path = $w.Document.Folder.Self.Path
            Write-Output ($w.LocationName + "`t" + $path)
        }
    } catch {}
}
'''


def _win_folder_at(x, y):
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.GetAncestor.restype = wintypes.HWND
    hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    if not hwnd:
        return None, "", NOT_HERE.format(fm=_fm())
    top = user32.GetAncestor(hwnd, 2)  # GA_ROOT
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(top, cls, 256)
    if cls.value in ("Progman", "WorkerW"):
        return _desktop_windows(), "Schreibtisch", None
    if cls.value != "CabinetWClass":
        return None, "", NOT_HERE.format(fm=_fm())
    title = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(top, title, 512)
    script = _PS_SCRIPT.replace("{hwnd}", str(int(top)))
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, encoding="utf-8", timeout=10,
                            creationflags=0x08000000)  # CREATE_NO_WINDOW
    candidates = []
    for line in result.stdout.splitlines():
        if "\t" in line:
            name, path = line.split("\t", 1)
            if path and not path.startswith("::") and os.path.isdir(path):
                candidates.append((name, path))
    if not candidates:
        return None, "", ("Dieses Explorer-Fenster zeigt keinen normalen Ordner "
                          "(z. B. „Dieser PC“ oder „Start“).")
    # Windows 11: mehrere Tabs teilen ein Fenster – den aktiven über den Fenstertitel finden
    for name, path in candidates:
        if name and title.value.startswith(name):
            return path, name, None
    name, path = candidates[0]
    return path, name or os.path.basename(path), None

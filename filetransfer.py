"""
HopGrid – Dateien und Ordner über den verschlüsselten Kanal übertragen.

Ablauf einer Übertragung (beide Richtungen gleich):
    MSG_XFER_START  JSON {id, count, bytes, names}     – Überblick für Fortschritt/Speicherplatz
    MSG_XFER_ITEM   JSON {id, path, size, dir}          – je Datei/Ordner, path relativ, mit "/"
    MSG_XFER_DATA   4 Byte id + Datenblock              – Inhalt der aktuellen Datei
    MSG_XFER_END    JSON {id, ok, error}                – Absender ist fertig (oder abgebrochen)
    MSG_XFER_ACK    JSON {id, ok, folder, error}        – Empfänger meldet zurück, wo es liegt

Sicherheit: Pfade werden streng geprüft (keine absoluten Pfade, kein "..", keine
Laufwerksbuchstaben), es wird nie etwas überschrieben, und vor dem Schreiben wird
der freie Speicherplatz geprüft.
"""

import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time

import protocol as proto

CHUNK = 256 * 1024
TARGET_SUBFOLDER = "HopGrid"
_ID = struct.Struct("!I")
_INVALID = '<>:"/\\|?*'


def downloads_dir():
    """Ablageort für empfangene Dateien: Downloads/HopGrid."""
    base = os.path.join(os.path.expanduser("~"), "Downloads")
    if not os.path.isdir(base):
        base = os.path.expanduser("~")
    return os.path.join(base, TARGET_SUBFOLDER)


def reveal(paths):
    """Im Finder/Explorer zeigen (erste Datei markiert)."""
    if not paths:
        return
    target = paths[0]
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", target])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(target)])
    except OSError:
        pass


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024


def describe(names, count):
    """'bericht.pdf' oder 'bericht.pdf und 2 weitere'."""
    if not names:
        return f"{count} Dateien"
    first = names[0]
    rest = count - 1 if count > len(names) else len(names) - 1
    return first if rest <= 0 else f"{first} und {rest} weitere"


# ---------------------------------------------------------------------------
# Senden
# ---------------------------------------------------------------------------
def collect(paths):
    """Dateien/Ordner einsammeln. Liefert [(quelle|None, relativer_pfad, größe, ist_ordner)].
    Symlinks und Sonderdateien werden übersprungen."""
    entries = []
    for path in paths:
        path = os.path.abspath(path)
        top = os.path.basename(path.rstrip(os.sep)) or "Laufwerk"
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            entries.append((path, top, info.st_size, False))
        elif stat.S_ISDIR(info.st_mode):
            entries.append((None, top, 0, True))
            for root, dirs, files in os.walk(path):
                dirs.sort()
                rel_root = os.path.relpath(root, os.path.dirname(path)).replace(os.sep, "/")
                for name in dirs:
                    full = os.path.join(root, name)
                    if not os.path.islink(full):
                        entries.append((None, f"{rel_root}/{name}", 0, True))
                for name in sorted(files):
                    full = os.path.join(root, name)
                    try:
                        st = os.lstat(full)
                    except OSError:
                        continue
                    if stat.S_ISREG(st.st_mode):
                        entries.append((full, f"{rel_root}/{name}", st.st_size, False))
    return entries


def send_paths(channel, paths, on_progress=None, cancel=None):
    """Überträgt Dateien/Ordner. Blockiert bis zum Ende (in eigenem Thread aufrufen).
    on_progress(gesendet, gesamt). Liefert die Übertragungs-ID."""
    entries = collect(paths)
    total = sum(e[2] for e in entries)
    xfer_id = int(time.time() * 1000) & 0x7FFFFFFF
    names = [os.path.basename(p.rstrip(os.sep)) for p in paths][:5]
    count = sum(1 for e in entries if not e[3]) or len(entries)
    channel.send(proto.MSG_XFER_START, json.dumps(
        {"id": xfer_id, "count": count, "bytes": total, "names": names}).encode("utf-8"))
    sent = 0
    error = ""
    try:
        for source, rel, size, is_dir in entries:
            if cancel is not None and cancel.is_set():
                error = "abgebrochen"
                break
            channel.send(proto.MSG_XFER_ITEM, json.dumps(
                {"id": xfer_id, "path": rel, "size": size, "dir": is_dir}).encode("utf-8"))
            if is_dir:
                continue
            with open(source, "rb") as fh:
                while True:
                    block = fh.read(CHUNK)
                    if not block:
                        break
                    channel.send(proto.MSG_XFER_DATA, _ID.pack(xfer_id) + block)
                    sent += len(block)
                    if on_progress:
                        on_progress(sent, total)
    except OSError as exc:
        if isinstance(exc, (ConnectionError, BrokenPipeError)):
            raise
        error = f"Lesefehler: {exc}"
    channel.send(proto.MSG_XFER_END, json.dumps(
        {"id": xfer_id, "ok": not error, "error": error}).encode("utf-8"))
    return xfer_id


# ---------------------------------------------------------------------------
# Empfangen
# ---------------------------------------------------------------------------
def _safe_parts(rel):
    parts = []
    for part in str(rel).replace("\\", "/").split("/"):
        if part in ("", ".", ".."):
            if part == "..":
                raise ValueError("unzulässiger Pfad")
            continue
        clean = "".join("_" if ch in _INVALID or ord(ch) < 32 else ch for ch in part).strip(" .")
        if not clean:
            raise ValueError("unzulässiger Name")
        parts.append(clean[:200])
    if not parts:
        raise ValueError("leerer Pfad")
    return parts


def _unique(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


class Receiver:
    """Nimmt Übertragungen entgegen und schreibt sie nach dest_dir.
    on_event(art, daten) mit art = 'start' | 'progress' | 'done'."""

    def __init__(self, channel, dest_dir, on_event=None):
        self.channel = channel
        self.dest_dir = dest_dir
        self.on_event = on_event or (lambda kind, data: None)
        self._lock = threading.Lock()
        self._xfer = None

    def handle(self, msg_type, payload):
        """Liefert True, wenn die Nachricht zur Dateiübertragung gehörte."""
        if msg_type not in (proto.MSG_XFER_START, proto.MSG_XFER_ITEM,
                            proto.MSG_XFER_DATA, proto.MSG_XFER_END):
            return False
        with self._lock:
            try:
                if msg_type == proto.MSG_XFER_DATA:
                    self._data(payload)
                else:
                    data = json.loads(payload)
                    {proto.MSG_XFER_START: self._start, proto.MSG_XFER_ITEM: self._item,
                     proto.MSG_XFER_END: self._end}[msg_type](data)
            except Exception as exc:  # Fehler betrifft nur diese Übertragung
                self._fail(str(exc))
        return True

    def _start(self, data):
        self._close_file()
        total = int(data.get("bytes", 0))
        if not self.dest_dir:
            self._xfer = {"id": int(data["id"]), "total": total, "done": 0, "tops": {},
                          "saved": [], "file": None, "remaining": 0, "failed": "",
                          "names": data.get("names", []), "count": int(data.get("count", 0)),
                          "last_event": 0.0}
            raise OSError("kein Zielordner – Dateien bitte auf den Schreibtisch oder in ein "
                          "Ordnerfenster ziehen")
        os.makedirs(self.dest_dir, exist_ok=True)
        free = shutil.disk_usage(self.dest_dir).free
        self._xfer = {"id": int(data["id"]), "total": total, "done": 0, "tops": {},
                      "saved": [], "file": None, "remaining": 0, "failed": "",
                      "names": data.get("names", []), "count": int(data.get("count", 0)),
                      "last_event": 0.0}
        self.on_event("start", {"names": self._xfer["names"], "count": self._xfer["count"],
                                "bytes": total})
        if total > free - 50 * 1024 * 1024:
            raise OSError(f"zu wenig Speicherplatz ({human_size(free)} frei, "
                          f"{human_size(total)} nötig)")

    def _target(self, rel):
        parts = _safe_parts(rel)
        tops = self._xfer["tops"]
        if parts[0] not in tops:  # oberste Ebene: nie überschreiben, ggf. "(2)" anhängen
            top_path = _unique(os.path.join(self.dest_dir, parts[0]))
            tops[parts[0]] = top_path
            self._xfer["saved"].append(top_path)
        return os.path.join(tops[parts[0]], *parts[1:])

    def _item(self, data):
        x = self._xfer
        if x is None or x["failed"] or int(data["id"]) != x["id"]:
            return
        self._close_file()
        path = self._target(data["path"])
        if data.get("dir"):
            os.makedirs(path, exist_ok=True)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        x["file"] = open(_unique(path), "wb")
        x["remaining"] = int(data.get("size", 0))

    def _data(self, payload):
        x = self._xfer
        if x is None or x["failed"] or x["file"] is None:
            return
        (xfer_id,) = _ID.unpack_from(payload)
        if xfer_id != x["id"]:
            return
        block = payload[_ID.size:]
        x["file"].write(block)
        x["done"] += len(block)
        now = time.monotonic()
        if now - x["last_event"] > 0.15:
            x["last_event"] = now
            self.on_event("progress", {"done": x["done"], "total": x["total"]})

    def _end(self, data):
        x = self._xfer
        if x is None or int(data["id"]) != x["id"]:
            return
        self._close_file()
        error = x["failed"] or ("" if data.get("ok") else str(data.get("error") or "abgebrochen"))
        self._finish(error)

    def _fail(self, error):
        if self._xfer is not None and not self._xfer["failed"]:
            self._xfer["failed"] = error
            self._close_file()

    def _finish(self, error):
        x = self._xfer
        self._xfer = None
        result = {"ok": not error, "error": error, "saved": x["saved"], "folder": self.dest_dir,
                  "names": x["names"], "count": x["count"], "bytes": x["done"]}
        try:
            self.channel.send(proto.MSG_XFER_ACK, json.dumps(
                {"id": x["id"], "ok": not error, "error": error,
                 "folder": self.dest_dir}).encode("utf-8"))
        except OSError:
            pass
        self.on_event("done", result)

    def _close_file(self):
        if self._xfer is not None and self._xfer.get("file") is not None:
            try:
                self._xfer["file"].close()
            except OSError:
                pass
            self._xfer["file"] = None


# ---------------------------------------------------------------------------
# Kopierte Dateien in der Zwischenablage (Host -> Client)
# ---------------------------------------------------------------------------
def clipboard_files():
    """Pfade der Dateien, die gerade in Finder/Explorer kopiert sind (sonst [])."""
    try:
        if sys.platform == "darwin":
            from AppKit import NSPasteboard, NSURL
            board = NSPasteboard.generalPasteboard()
            urls = board.readObjectsForClasses_options_(
                [NSURL], {"NSPasteboardURLReadingFileURLsOnlyKey": True}) or []
            return [str(u.path()) for u in urls if u.path()]
        if sys.platform == "win32":
            return _win_clipboard_files()
    except Exception:
        pass
    return []


def _win_clipboard_files():
    import ctypes
    from ctypes import wintypes
    user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
    CF_HDROP = 15
    if not user32.IsClipboardFormatAvailable(CF_HDROP):
        return []
    if not user32.OpenClipboard(None):
        return []
    try:
        user32.GetClipboardData.restype = wintypes.HANDLE
        handle = user32.GetClipboardData(CF_HDROP)
        if not handle:
            return []
        shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                           wintypes.LPWSTR, wintypes.UINT]
        count = shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
        paths = []
        for i in range(count):
            length = shell32.DragQueryFileW(handle, i, None, 0) + 1
            buf = ctypes.create_unicode_buffer(length)
            shell32.DragQueryFileW(handle, i, buf, length)
            paths.append(buf.value)
        return paths
    finally:
        user32.CloseClipboard()

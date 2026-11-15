#!/usr/bin/env python3
"""
HopGrid Host – teilt den Bildschirm im lokalen Netz und führt Maus-/Tastatureingaben
eines verbundenen Clients aus. Kein externer Server, direkte, verschlüsselte TCP-Verbindung.

Start:
    python host.py                       # Fenster (Standard)
    python host.py --cli                 # Terminal-Modus, fragt Benutzername/Passwort ab
    python host.py --port 8080 --fps 15 --quality 85 --max-width 2560 --monitor 1
"""

import argparse
import getpass
import hmac
import json
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk


from dpi import enable_dpi_awareness  # noqa: E402

enable_dpi_awareness()

import cv2  # noqa: E402
import mss  # noqa: E402
import numpy as np  # noqa: E402
from pynput.keyboard import Controller as KeyboardController  # noqa: E402
from pynput.keyboard import Key, KeyCode  # noqa: E402
from pynput.mouse import Button  # noqa: E402
from pynput.mouse import Controller as MouseController  # noqa: E402

import autostart  # noqa: E402
import customtkinter as ctk  # noqa: E402
import ui  # noqa: E402
import protocol as proto  # noqa: E402

if sys.platform == "darwin":
    import Quartz  # wird mit pynput (pyobjc) installiert

# mss >= 10 heißt die Klasse MSS, ältere Versionen nutzen mss.mss
MSS = getattr(mss, "MSS", None) or mss.mss

MAX_INPUT_MSG = 2 * 1024 * 1024  # Eingaben + Zwischenablage; schützt vor Speichermissbrauch
AUTH_TIMEOUT = 15            # Sekunden für den kompletten Login
FAIL_DELAY = 2.0             # Verzögerung nach falschem Login (bremst Raten)
MAX_FAILS = 5                # Fehlversuche pro IP bis zur Sperre
BLOCK_SECONDS = 300          # Dauer der IP-Sperre
KEEPALIVE_SECONDS = 2.0      # spätestens so oft ein Bild oder Lebenszeichen senden
SESSION_TIMEOUT = 20         # ohne Lebenszeichen vom Client gilt die Verbindung als tot
TAKEOVER_WAIT = 5            # so lange auf das Ende einer alten Sitzung warten
MIN_PASSWORD_LEN = 8
HOST_CONFIG = "host.json"   # gespeicherte Freigabe für den Autostart


# ---------------------------------------------------------------------------
# Protokollausgabe (Terminal oder Fenster)
# ---------------------------------------------------------------------------
_log_handler = print


def log(message):
    _log_handler(message)


def set_log_handler(handler):
    global _log_handler
    _log_handler = handler


_status_handler = None


def set_status_handler(handler):
    """handler(event, ip) mit event = 'connected' oder 'disconnected' (für die Oberfläche)."""
    global _status_handler
    _status_handler = handler


def notify(event, ip):
    if _status_handler is not None:
        try:
            _status_handler(event, ip)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Zugangsdaten und Schutz gegen Passwort-Raten
# ---------------------------------------------------------------------------
class Credentials:
    """Hält nur den abgeleiteten Schlüssel, nicht das Klartext-Passwort."""

    def __init__(self, username, password):
        self.username = username
        self.salt = os.urandom(16)
        self.key = proto.derive_password_key(password, self.salt, proto.PBKDF2_ITERATIONS)

    @classmethod
    def from_stored(cls, username, salt, key):
        """Zugangsdaten aus der Autostart-Datei (Salt + abgeleiteter Schlüssel)."""
        if not username or len(salt) != 16 or len(key) != 32:
            raise ValueError("Gespeicherte Zugangsdaten sind unvollständig")
        creds = cls.__new__(cls)
        creds.username, creds.salt, creds.key = username, salt, key
        return creds

    def verify(self, username, client_proof, transcript):
        expected = proto.make_proof(self.key, b"client", transcript)
        user_ok = hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8"))
        proof_ok = hmac.compare_digest(expected, client_proof)
        return user_ok and proof_ok


class FailTracker:
    """Zählt Fehlversuche pro IP und sperrt nach MAX_FAILS für BLOCK_SECONDS."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {}  # ip -> (fehlversuche, gesperrt_bis)

    def is_blocked(self, ip):
        with self._lock:
            _, until = self._state.get(ip, (0, 0.0))
            return time.monotonic() < until

    def register_fail(self, ip):
        with self._lock:
            count, until = self._state.get(ip, (0, 0.0))
            count += 1
            if count >= MAX_FAILS:
                self._state[ip] = (0, time.monotonic() + BLOCK_SECONDS)
                log(f"[!] {ip} für {BLOCK_SECONDS} s gesperrt (zu viele Fehlversuche)")
            else:
                self._state[ip] = (count, until)

    def reset(self, ip):
        with self._lock:
            self._state.pop(ip, None)


# Offene Client-Verbindungen, damit "Freigabe stoppen" sie sofort trennen kann
_active_conns = set()
_active_lock = threading.Lock()
_session_conn = [None]  # Verbindung der laufenden Sitzung (für Übernahme bei neuem Login)


def close_active_connections():
    with _active_lock:
        conns = list(_active_conns)
    for conn in conns:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Eingaben ausführen
# ---------------------------------------------------------------------------
class MacMouse:
    """macOS: Maustasten direkt über Quartz senden. pynput setzt beim einzelnen
    press/release keinen Klickzähler, dadurch würden Doppelklicks nicht erkannt.
    Außerdem braucht Ziehen (Drag) eigene Ereignistypen."""

    TYPES = {
        "left": ("kCGEventLeftMouseDown", "kCGEventLeftMouseUp",
                 "kCGEventLeftMouseDragged", "kCGMouseButtonLeft"),
        "right": ("kCGEventRightMouseDown", "kCGEventRightMouseUp",
                  "kCGEventRightMouseDragged", "kCGMouseButtonRight"),
        "middle": ("kCGEventOtherMouseDown", "kCGEventOtherMouseUp",
                   "kCGEventOtherMouseDragged", "kCGMouseButtonCenter"),
    }

    def __init__(self):
        self.held = []  # aktuell gedrückte Tasten in Reihenfolge

    def _post(self, event_type_name, button_name, x, y, clicks=None):
        button = getattr(Quartz, self.TYPES[button_name][3])
        event = Quartz.CGEventCreateMouseEvent(
            None, getattr(Quartz, event_type_name), (x, y), button
        )
        if clicks is not None:
            Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, clicks)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def move(self, x, y):
        if self.held:
            name = self.held[-1]
            self._post(self.TYPES[name][2], name, x, y)
        else:
            event = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def press(self, name, x, y, clicks):
        self._post(self.TYPES[name][0], name, x, y, clicks)
        if name not in self.held:
            self.held.append(name)

    def release(self, name, x, y, clicks):
        self._post(self.TYPES[name][1], name, x, y, clicks)
        if name in self.held:
            self.held.remove(name)


class MacKeyboard:
    """macOS: Tastatur direkt über Quartz.

    pynput fragt beim Tippen das Tastaturlayout über TIS-Funktionen ab. Die dürfen auf
    neueren macOS-Versionen nur im Hauptthread laufen; im Empfangs-Thread kann das hängen
    und die Steuerung blockieren. Hier: Zeichen als Unicode senden (layoutunabhängig),
    bei gedrückter Cmd/Strg/Alt-Taste über den Tastencode (für Kürzel wie Cmd+C)."""

    SPECIAL = {
        "enter": 36, "tab": 48, "space": 49, "backspace": 51, "esc": 53,
        "cmd": 55, "cmd_r": 54, "shift": 56, "shift_r": 60, "caps_lock": 57,
        "alt": 58, "alt_r": 61, "alt_gr": 61, "ctrl": 59, "ctrl_r": 62,
        "home": 115, "page_up": 116, "delete": 117, "end": 119, "page_down": 121,
        "left": 123, "right": 124, "down": 125, "up": 126,
        "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
        "f9": 101, "f10": 109, "f11": 103, "f12": 111, "f13": 105, "f14": 107, "f15": 113,
        "f16": 106, "f17": 64, "f18": 79, "f19": 80, "f20": 90,
    }
    # US-ANSI-Tastencodes für Kürzel (Cmd+C usw.)
    ANSI = {
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
        "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
        "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
        "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38,
        "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47,
        "`": 50,
    }
    FLAGS = {"shift": 0x20000, "shift_r": 0x20000, "ctrl": 0x40000, "ctrl_r": 0x40000,
             "alt": 0x80000, "alt_r": 0x80000, "alt_gr": 0x80000,
             "cmd": 0x100000, "cmd_r": 0x100000}

    def __init__(self):
        self.held_modifiers = set()

    def _flags(self):
        flags = 0
        for name in self.held_modifiers:
            flags |= self.FLAGS[name]
        return flags

    def _post(self, keycode, down, text=None):
        event = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
        if text is not None:
            Quartz.CGEventKeyboardSetUnicodeString(event, len(text), text)
        Quartz.CGEventSetFlags(event, self._flags())
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def key(self, name, down):
        """Sondertaste. Liefert False, wenn unbekannt."""
        code = self.SPECIAL.get(name)
        if code is None:
            return False
        if name in self.FLAGS:
            if down:
                self.held_modifiers.add(name)
            else:
                self.held_modifiers.discard(name)
        self._post(code, down)
        return True

    def char(self, ch, down):
        shortcut = self.held_modifiers - {"shift", "shift_r"}
        code = self.ANSI.get(ch.lower())
        if shortcut and code is not None:
            self._post(code, down)            # Kürzel: echter Tastencode
        else:
            self._post(code if code is not None else 0, down, text=ch)  # Text: Unicode

    def release_all(self):
        for name in list(self.held_modifiers):
            self.key(name, False)


class InputExecutor:
    """Setzt die JSON-Ereignisse des Clients in echte Maus-/Tastatureingaben um.

    Mauskoordinaten kommen normiert (0..1) und werden auf den geteilten Monitor
    umgerechnet. So passt es unabhängig von Fenstergröße und Retina-Skalierung."""

    BUTTONS = {"left": Button.left, "right": Button.right, "middle": Button.middle}
    DOUBLE_CLICK_TIME = 0.5   # Sekunden
    DOUBLE_CLICK_DIST = 6     # Pixel

    def __init__(self, monitor):
        self.monitor = monitor
        self.mouse = MouseController()
        self.keyboard = KeyboardController()
        self.mac_mouse = MacMouse() if sys.platform == "darwin" else None
        self.mac_keyboard = MacKeyboard() if sys.platform == "darwin" else None
        self.pressed_keys = set()
        self.pressed_buttons = set()
        self._last_click = (None, 0.0, 0, 0, 0)  # Taste, Zeit, x, y, Zähler
        self._position = (monitor["left"], monitor["top"])

    def _to_screen(self, nx, ny):
        nx = min(max(float(nx), 0.0), 1.0)
        ny = min(max(float(ny), 0.0), 1.0)
        x = self.monitor["left"] + nx * (self.monitor["width"] - 1)
        y = self.monitor["top"] + ny * (self.monitor["height"] - 1)
        return int(round(x)), int(round(y))

    def _click_count(self, name, x, y):
        """Zählt schnelle Klicks an gleicher Stelle (nur für macOS nötig)."""
        last_name, last_time, lx, ly, count = self._last_click
        now = time.monotonic()
        if (name == last_name and now - last_time < self.DOUBLE_CLICK_TIME
                and abs(x - lx) <= self.DOUBLE_CLICK_DIST
                and abs(y - ly) <= self.DOUBLE_CLICK_DIST):
            count += 1
        else:
            count = 1
        self._last_click = (name, now, x, y, count)
        return count

    @staticmethod
    def _resolve_key(event):
        if "key" in event:
            name = str(event["key"])
            # Nur echte Key-Mitglieder zulassen (z. B. Key.insert fehlt auf macOS)
            return Key[name] if name in Key.__members__ else None
        if "char" in event:
            ch = str(event["char"])
            if len(ch) == 1 and ch.isprintable():
                return KeyCode.from_char(ch)
        return None

    def _move(self, x, y):
        self._position = (x, y)
        if self.mac_mouse:
            self.mac_mouse.move(x, y)
        else:
            self.mouse.position = (x, y)

    def handle(self, event):
        kind = event.get("t")

        if kind == "move":
            self._move(*self._to_screen(event["x"], event["y"]))

        elif kind == "btn":
            name = event.get("b")
            if name not in self.BUTTONS:
                return
            x, y = self._to_screen(event["x"], event["y"])
            down = event.get("a") == "down"
            if self.mac_mouse:
                clicks = self._click_count(name, x, y) if down else self._last_click[4]
                self._position = (x, y)
                if down:
                    self.mac_mouse.press(name, x, y, clicks)
                    self.pressed_buttons.add(name)
                else:
                    self.mac_mouse.release(name, x, y, max(clicks, 1))
                    self.pressed_buttons.discard(name)
            else:
                self._move(x, y)
                if down:
                    self.mouse.press(self.BUTTONS[name])
                    self.pressed_buttons.add(name)
                else:
                    self.mouse.release(self.BUTTONS[name])
                    self.pressed_buttons.discard(name)

        elif kind == "scroll":
            dy = max(-20, min(20, int(event.get("dy", 0))))
            if dy:
                self.mouse.scroll(0, dy)

        elif kind == "key" and self.mac_keyboard is not None:
            down = event.get("a") == "down"
            if "key" in event:
                self.mac_keyboard.key(str(event["key"]), down)
            elif "char" in event:
                ch = str(event["char"])
                if len(ch) == 1 and ch.isprintable():
                    self.mac_keyboard.char(ch, down)

        elif kind == "key":
            key = self._resolve_key(event)
            if key is None:
                return
            if event.get("a") == "down":
                self.keyboard.press(key)
                self.pressed_keys.add(key)
            else:
                self.keyboard.release(key)
                self.pressed_keys.discard(key)

    def release_all(self):
        """Nach Verbindungsende alles loslassen, damit z. B. Strg nicht 'hängt'."""
        if self.mac_keyboard is not None:
            try:
                self.mac_keyboard.release_all()
            except Exception:
                pass
        for key in list(self.pressed_keys):
            try:
                self.keyboard.release(key)
            except Exception:
                pass
        self.pressed_keys.clear()
        x, y = self._position
        for name in list(self.pressed_buttons):
            try:
                if self.mac_mouse:
                    self.mac_mouse.release(name, x, y, 1)
                else:
                    self.mouse.release(self.BUTTONS[name])
            except Exception:
                pass
        self.pressed_buttons.clear()


# ---------------------------------------------------------------------------
# Bildschirm aufnehmen und senden
# ---------------------------------------------------------------------------
def stream_frames(channel, stop, args, state=None):
    """Läuft in eigenem Thread: Bildschirm aufnehmen und senden.

    Fehler bei der Aufnahme (Sperrbildschirm, UAC-Abfrage, Monitorwechsel) beenden
    die Verbindung NICHT: Es wird neu versucht und solange ein Lebenszeichen gesendet.
    Nur ein Fehler beim Senden (Verbindung weg) beendet die Sitzung."""
    interval = 1.0 / args.fps
    encode_params = [cv2.IMWRITE_JPEG_QUALITY, args.quality]
    # Volle Farbauflösung (4:4:4) statt 4:2:0: Schrift bleibt scharf, farbige Ränder verschwinden
    if hasattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR_444"):
        encode_params += [cv2.IMWRITE_JPEG_SAMPLING_FACTOR, cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444]

    sct = None
    monitor = None
    last_img = None
    last_sent = 0.0
    capture_failed = False
    try:
        while not stop.is_set():
            started = time.monotonic()
            jpeg = None
            try:
                if sct is None:  # mss-Instanz muss im selben Thread entstehen
                    sct = MSS()
                    index = args.monitor if args.monitor < len(sct.monitors) else 1
                    monitor = sct.monitors[index]
                img = cv2.cvtColor(np.asarray(sct.grab(monitor)), cv2.COLOR_BGRA2BGR)
                h, w = img.shape[:2]
                if w > args.max_width:                         # Bandbreite sparen
                    img = cv2.resize(img, (args.max_width, int(h * args.max_width / w)),
                                     interpolation=cv2.INTER_AREA)
                changed = last_img is None or not np.array_equal(img, last_img)
                if changed or started - last_sent >= KEEPALIVE_SECONDS:
                    ok, encoded = cv2.imencode(".jpg", img, encode_params)
                    if ok:
                        jpeg = encoded.tobytes()
                        last_img = img
                if capture_failed:
                    log("[*] Bildschirmaufnahme läuft wieder")
                    capture_failed = False
                    if state is not None:
                        state["capture_failed"] = False
            except Exception as exc:
                if not capture_failed:
                    log(f"[!] Bildschirm gerade nicht lesbar ({exc}). Verbindung bleibt, "
                        "neuer Versuch läuft.")
                    capture_failed = True
                    if state is not None:
                        state["capture_failed"] = True
                if sct is not None:
                    try:
                        sct.close()
                    except Exception:
                        pass
                    sct = None
                last_img = None

            if jpeg is not None:
                channel.send(proto.MSG_FRAME, jpeg)
                last_sent = started
            elif started - last_sent >= KEEPALIVE_SECONDS:
                channel.send(proto.MSG_PING, b"")
                last_sent = started

            pause = 1.0 if capture_failed else interval - (time.monotonic() - started)
            stop.wait(max(0.0, pause))
    except (OSError, ConnectionError) as exc:
        if not stop.is_set():
            log(f"[-] Senden abgebrochen: {exc}")
    finally:
        if sct is not None:
            try:
                sct.close()
            except Exception:
                pass
        stop.set()
        try:
            channel.sock.shutdown(socket.SHUT_RDWR)  # weckt den Empfangs-Thread auf
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Verbindung eines Clients
# ---------------------------------------------------------------------------
class ClipboardSync:
    """Gleicht die Text-Zwischenablage des Hosts mit dem Client ab.
    Ein eigener Thread meldet Änderungen am Host, set_from_client übernimmt Text vom Client."""

    def __init__(self, channel, stop):
        self.channel = channel
        self.stop = stop
        self._lock = threading.Lock()
        try:
            import pyperclip
            self._clip = pyperclip
            self._last = pyperclip.paste()
        except Exception as exc:
            log(f"[!] Zwischenablage nicht verfügbar: {exc}")
            self._clip = None
            return
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        while not self.stop.wait(0.5):
            try:
                text = self._clip.paste()
            except Exception:
                continue
            with self._lock:
                if not text or text == self._last:
                    continue
                self._last = text
            data = text.encode("utf-8")
            if len(data) <= proto.MAX_CLIPBOARD:
                try:
                    self.channel.send(proto.MSG_CLIPBOARD, data)
                except OSError:
                    return

    def set_from_client(self, payload):
        if self._clip is None or len(payload) > proto.MAX_CLIPBOARD:
            return
        text = payload.decode("utf-8", errors="replace")
        with self._lock:
            self._last = text
        try:
            self._clip.copy(text)
        except Exception as exc:
            log(f"[!] Zwischenablage konnte nicht gesetzt werden: {exc}")


def _process_elevated(pid):
    """Windows: Läuft der Prozess als Administrator? Zugriff verweigert zählt als ja."""
    import ctypes
    from ctypes import wintypes
    kernel32, advapi32 = ctypes.windll.kernel32, ctypes.windll.advapi32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return ctypes.GetLastError() == 5  # ERROR_ACCESS_DENIED
        try:
            elevation, size = wintypes.DWORD(), wintypes.DWORD()
            ok = advapi32.GetTokenInformation(token, 20, ctypes.byref(elevation),  # TokenElevation
                                              ctypes.sizeof(elevation), ctypes.byref(size))
            return bool(ok and elevation.value)
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(handle)


def control_problem(state):
    """Warum die Steuerung gerade nicht ankommt – oder '' wenn alles passt."""
    if sys.platform == "darwin":
        try:
            from ApplicationServices import AXIsProcessTrusted
            if not AXIsProcessTrusted():
                return ("Steuerung blockiert: Auf dem Host fehlt die Freigabe „Bedienungshilfen“. "
                        "Dort unter Systemeinstellungen › Datenschutz & Sicherheit › "
                        "Bedienungshilfen HopGrid entfernen (–) und neu hinzufügen (+).")
        except ImportError:
            pass
    elif sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            if not _process_elevated(os.getpid()):
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                pid = wintypes.DWORD()
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if hwnd and pid.value and _process_elevated(pid.value):
                    return ("Steuerung blockiert: Das aktive Fenster auf dem Host läuft als "
                            "Administrator. Abhilfe: HopGrid auf dem Host per Rechtsklick › "
                            "„Als Administrator ausführen“ starten.")
        except Exception:
            pass
    if state.get("capture_failed"):
        return ("Der Bildschirm des Hosts ist gerade nicht lesbar (Sperrbildschirm oder "
                "Sicherheitsabfrage). Die Verbindung bleibt bestehen.")
    return ""


def watch_cursor_and_control(channel, stop, monitor, state):
    """Sendet die Mausposition des Hosts (ca. 30x pro Sekunde, nur bei Änderung)
    und Hinweise, wenn die Steuerung blockiert ist."""
    mouse = MouseController()
    last_pos = None
    last_notice = None
    next_check = 0.0
    try:
        while not stop.wait(1 / 30):
            try:
                x, y = mouse.position
                nx = (x - monitor["left"]) / max(1, monitor["width"])
                ny = (y - monitor["top"]) / max(1, monitor["height"])
                visible = 0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0
                pos = (round(nx, 4), round(ny, 4), visible)
            except Exception:
                pos = (0.0, 0.0, False)
            if pos != last_pos:
                last_pos = pos
                channel.send(proto.MSG_CURSOR, json.dumps(
                    {"x": pos[0], "y": pos[1], "v": pos[2]}).encode("utf-8"))
            now = time.monotonic()
            if now >= next_check:
                next_check = now + 2.0
                notice = control_problem(state)
                if notice != last_notice:
                    last_notice = notice
                    if notice:
                        log(f"[!] {notice}")
                    channel.send(proto.MSG_NOTICE, notice.encode("utf-8"))
    except (OSError, ConnectionError):
        pass


def run_session(channel, ip, args, monitor):
    stop = threading.Event()
    state = {"capture_failed": False}
    executor = InputExecutor(monitor)
    sender = threading.Thread(target=stream_frames, args=(channel, stop, args, state),
                              daemon=True)
    sender.start()
    threading.Thread(target=watch_cursor_and_control, args=(channel, stop, monitor, state),
                     daemon=True).start()
    clipboard = ClipboardSync(channel, stop)
    try:
        while not stop.is_set():
            msg_type, payload = channel.recv()
            if msg_type == proto.MSG_CLIPBOARD:
                clipboard.set_from_client(payload)
                continue
            if msg_type != proto.MSG_INPUT:
                continue
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            try:
                executor.handle(event)
            except Exception as exc:  # ein fehlerhaftes Ereignis darf die Sitzung nicht beenden
                log(f"[!] Eingabe von {ip} nicht ausführbar ({exc}), ignoriert")
    finally:
        stop.set()
        executor.release_all()
        sender.join(timeout=3)


def handle_client(conn, addr, creds, tracker, args, monitor, session_lock):
    ip = addr[0]
    with _active_lock:
        _active_conns.add(conn)
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    conn.settimeout(AUTH_TIMEOUT)
    try:
        # 1. Challenge mit Einmal-Nonce und kurzlebigem Schlüssel senden
        nonce = os.urandom(32)
        kx = proto.KeyExchange()
        proto.send_plain(conn, proto.MSG_CHALLENGE, {
            "v": proto.PROTOCOL_VERSION,
            "salt": creds.salt.hex(),
            "iterations": proto.PBKDF2_ITERATIONS,
            "nonce": nonce.hex(),
            "pub": kx.public.hex(),
        })

        # 2. Antwort des Clients prüfen
        response = proto.recv_plain(conn, proto.MSG_AUTH_RESPONSE)
        username = str(response.get("user", ""))[:128]
        client_pub = proto.hex_field(response, "pub", 32)
        client_proof = proto.hex_field(response, "proof", 32)
        transcript = proto.build_transcript(nonce, kx.public, client_pub, username)

        if not creds.verify(username, client_proof, transcript):
            tracker.register_fail(ip)
            log(f"[-] Fehlgeschlagener Login von {ip}")
            time.sleep(FAIL_DELAY)
            proto.send_plain(conn, proto.MSG_AUTH_RESULT,
                             {"ok": False, "reason": "Benutzername oder Passwort falsch."})
            return

        tracker.reset(ip)
        # Neuer erfolgreicher Login übernimmt: alte (evtl. tote) Verbindung trennen.
        # Wichtig für das automatische Neuverbinden nach einem Netzaussetzer.
        if not session_lock.acquire(blocking=False):
            log(f"[*] Neuer Login von {ip} – bisherige Verbindung wird getrennt")
            with _active_lock:
                current = _session_conn[0]
            if current is not None:
                try:
                    current.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            if not session_lock.acquire(timeout=TAKEOVER_WAIT):
                proto.send_plain(conn, proto.MSG_AUTH_RESULT,
                                 {"ok": False, "reason": "Der Host ist gerade belegt."})
                return
        with _active_lock:
            _session_conn[0] = conn
        try:
            # 3. Gegenbeweis senden, ab jetzt verschlüsselt
            host_to_client, client_to_host = kx.session_keys(client_pub, creds.key, transcript)
            proto.send_plain(conn, proto.MSG_AUTH_RESULT, {
                "ok": True,
                "proof": proto.make_proof(creds.key, b"host", transcript).hex(),
            })
            channel = proto.SecureChannel(conn, host_to_client, client_to_host, MAX_INPUT_MSG)
            conn.settimeout(SESSION_TIMEOUT)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            log(f"[+] {ip} verbunden (verschlüsselt)")
            notify("connected", ip)
            try:
                run_session(channel, ip, args, monitor)
            finally:
                notify("disconnected", ip)
        finally:
            with _active_lock:
                if _session_conn[0] is conn:
                    _session_conn[0] = None
            session_lock.release()
    except proto.ProtocolError as exc:
        log(f"[!] {ip}: {exc}")
    except (OSError, ConnectionError) as exc:
        log(f"[-] Verbindung zu {ip} beendet: {exc}")
    finally:
        with _active_lock:
            _active_conns.discard(conn)
        try:
            conn.close()
        except OSError:
            pass
        log(f"[-] {ip} getrennt")


is_local_address = proto.is_local_address
local_ip = proto.local_ip


def default_alias():
    """Rechnername ohne Domain, z. B. 'Daniels-MacBook-Pro'."""
    return proto.clean_alias(socket.gethostname().split(".")[0]) or "HopGrid"


class DiscoveryResponder:
    """Beantwortet Suchanfragen der Clients im LAN mit Alias und Port."""

    def __init__(self, alias, port, allow_public):
        self.alias = alias
        self.port = port
        self.allow_public = allow_public
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("", proto.DISCOVERY_PORT))
        self._sock.settimeout(1.0)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        reply = proto.discovery_reply(self.alias, self.port)
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if data != proto.DISCOVERY_REQUEST:
                continue
            if not self.allow_public and not is_local_address(addr[0]):
                continue
            try:
                self._sock.sendto(reply, addr)
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


def start_discovery(args):
    """Startet die Suche-Antwort. Ohne sie geht nur die Verbindung per IP."""
    try:
        responder = DiscoveryResponder(args.alias, args.port, args.allow_public)
        log(f"[*] Auffindbar als \"{args.alias}\"")
        return responder
    except OSError as exc:
        log(f"[!] Suche im LAN nicht verfügbar (UDP {proto.DISCOVERY_PORT}: {exc}). "
            "Verbinden per IP geht trotzdem.")
        return None


def prepare(args):
    """Prüft die Parameter und liefert die Daten des gewählten Monitors."""
    args.fps = max(1, min(60, args.fps))
    args.quality = max(10, min(95, args.quality))
    args.max_width = max(320, args.max_width)
    with MSS() as sct:
        monitors = sct.monitors
        if not 1 <= args.monitor < len(monitors):
            raise ValueError(
                f"Monitor {args.monitor} gibt es nicht. Verfügbar: 1-{len(monitors) - 1}")
        return dict(monitors[args.monitor])


def open_server(args):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((args.bind, args.port))
        server.listen(5)
    except OSError:
        server.close()
        raise
    server.settimeout(1.0)  # damit Stoppen/Strg+C auch unter Windows sofort greift
    return server


def serve(server, creds, args, monitor, stop_event):
    """Nimmt Verbindungen an, bis stop_event gesetzt oder der Socket geschlossen wird."""
    tracker = FailTracker()
    session_lock = threading.Lock()
    while not stop_event.is_set():
        try:
            conn, addr = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break  # Server-Socket wurde geschlossen
        ip = addr[0]
        if not args.allow_public and not is_local_address(ip):
            log(f"[!] {ip} abgelehnt (keine LAN-Adresse)")
            conn.close()
            continue
        if tracker.is_blocked(ip):
            conn.close()
            continue
        threading.Thread(
            target=handle_client,
            args=(conn, addr, creds, tracker, args, monitor, session_lock),
            daemon=True,
        ).start()


def check_macos_permissions():
    """Prüft die macOS-Freigaben und löst bei Bedarf die Systemabfrage aus."""
    if sys.platform != "darwin":
        return []
    hints = []
    try:
        if not Quartz.CGPreflightScreenCaptureAccess():
            Quartz.CGRequestScreenCaptureAccess()
            hints.append("Bildschirmaufnahme fehlt: Systemeinstellungen > Datenschutz & "
                         "Sicherheit > Bildschirm- & Systemaudioaufnahme > App erlauben, "
                         "dann App neu starten.")
    except AttributeError:
        pass
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
        if not AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}):
            hints.append("Bedienungshilfen fehlen: Systemeinstellungen > Datenschutz & "
                         "Sicherheit > Bedienungshilfen > App erlauben (sonst keine "
                         "Maus-/Tastatursteuerung).")
    except ImportError:
        pass
    return hints


# ---------------------------------------------------------------------------
# Terminal-Modus
# ---------------------------------------------------------------------------
def ask_credentials(args):
    username = (args.user or "").strip()
    while not username:
        username = input("Benutzername: ").strip()
    while True:
        password = getpass.getpass(f"Passwort (mind. {MIN_PASSWORD_LEN} Zeichen): ")
        if len(password) < MIN_PASSWORD_LEN:
            print("Zu kurz.")
            continue
        if getpass.getpass("Passwort wiederholen: ") != password:
            print("Passwörter stimmen nicht überein.")
            continue
        return username, password


def run_cli(args):
    try:
        monitor = prepare(args)
    except ValueError as exc:
        sys.exit(str(exc))
    username, password = ask_credentials(args)
    creds = Credentials(username, password)
    del password

    args.alias = proto.clean_alias(args.alias or default_alias())
    server = open_server(args)
    log(f"[*] HopGrid Host läuft. Verbinden mit {local_ip()}:{args.port}")
    discovery = start_discovery(args)
    log(f"[*] Monitor {args.monitor}: {monitor['width']}x{monitor['height']}, "
        f"{args.fps} FPS, JPEG {args.quality}. Beenden mit Strg+C.")
    for hint in check_macos_permissions():
        log(f"[!] {hint}")
    try:
        serve(server, creds, args, monitor, threading.Event())
    except KeyboardInterrupt:
        log("\n[*] Beendet.")
    finally:
        if discovery:
            discovery.stop()
        server.close()


# ---------------------------------------------------------------------------
# Fenster-Modus
# ---------------------------------------------------------------------------
resource_path = ui.resource_path


class KeepAwake:
    """Verhindert während der Freigabe den Ruhezustand des Rechners und
    (macOS) App Nap, das die App im Hintergrund drosseln würde."""

    def __init__(self):
        self._token = None

    def start(self):
        if sys.platform == "darwin":
            try:
                from Foundation import NSProcessInfo
                # NSActivityUserInitiated (inkl. kein Ruhezustand) | NSActivityLatencyCritical
                options = 0x00FFFFFF | 0xFF00000000
                self._token = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
                    options, "HopGrid-Freigabe aktiv")
            except Exception as exc:
                log(f"[!] Ruhezustand kann nicht verhindert werden: {exc}")
        elif sys.platform == "win32":
            import ctypes
            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)

    def stop(self):
        if sys.platform == "darwin" and self._token is not None:
            try:
                from Foundation import NSProcessInfo
                NSProcessInfo.processInfo().endActivity_(self._token)
            except Exception:
                pass
            self._token = None
        elif sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


class TrayIcon:
    """Windows: Symbol im Infobereich (unten rechts) mit Öffnen/Beenden."""

    def __init__(self, on_open, on_quit):
        import pystray
        from PIL import Image
        image = Image.open(resource_path(os.path.join("assets", "icon.png")))
        menu = pystray.Menu(
            pystray.MenuItem("Öffnen", lambda: on_open(), default=True),
            pystray.MenuItem("Beenden", lambda: on_quit()),
        )
        self._icon = pystray.Icon("HopGrid", image, "HopGrid – Freigabe aktiv", menu)
        threading.Thread(target=self._icon.run, daemon=True).start()

    def stop(self):
        try:
            self._icon.stop()
        except Exception:
            pass


class HostWindow:
    """Freigabe-Ansicht. autostart_launch=True: sofort mit gespeicherten Zugangsdaten
    freigeben und im Hintergrund laufen. on_back: Rückweg zur Startauswahl."""

    def __init__(self, root, args, autostart_launch=False, on_back=None):
        self.root = root
        self.args = args
        self.on_back = on_back
        self.server = None
        self.stop_event = None
        self.discovery = None
        self.creds = None
        self.tray = None
        self.alive = True
        self.keep_awake = KeepAwake()
        self.log_queue = queue.Queue()
        self.ui_queue = queue.Queue()   # Befehle aus anderen Threads (Infobereich, Status)
        set_log_handler(self.log_queue.put)
        set_status_handler(lambda event, ip: self.ui_queue.put(("status", event, ip)))

        saved = autostart.load_json(HOST_CONFIG)
        self.saved_creds = self._stored_creds(saved)

        root.title("HopGrid – Freigabe")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        if sys.platform == "darwin":
            root.createcommand("::tk::mac::Quit", self.on_close)
            root.createcommand("::tk::mac::ReopenApplication", self.show_window)

        self.frame = ctk.CTkFrame(root, fg_color=ui.BG)
        self.frame.pack(fill="both", expand=True)
        head = ui.header(self.frame, "Diesen Rechner freigeben",
                         "Ein anderer Rechner darf diesen Bildschirm sehen und steuern.",
                         on_back=self.go_back if on_back else None)
        self.back_button = getattr(head, "back_button", None)

        body = ctk.CTkFrame(self.frame, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=ui.S4, pady=(0, ui.S3))
        pad = {"padx": ui.S3 + 4, "pady": ui.S3}

        # --- Status: Plakette + Name in einer Zeile, darunter Adresse und Knopf ---
        status = ui.card(body)
        status.pack(fill="x", pady=(0, ui.S3))
        inner = ctk.CTkFrame(status, fg_color="transparent")
        inner.pack(fill="x", **pad)
        top = ctk.CTkFrame(inner, fg_color="transparent")
        top.pack(fill="x")
        self.headline = ui.label(top, "Freigabe ist aus", "h2", anchor="w")
        self.headline.pack(side="left")
        self.badge = ui.Badge(top)
        self.badge.widget.pack(side="right")
        self.subline = ui.label(inner, "Lege Zugangsdaten fest und starte die Freigabe.",
                                "small", ui.MUTED, anchor="w", justify="left", wraplength=440)
        self.subline.pack(anchor="w", pady=(2, ui.S2 + 4))
        self.status_inner = inner
        self.button = ui.primary_button(inner, "Freigabe starten", self.toggle)
        self.button.pack(fill="x")
        self.form_error = ui.label(inner, "", "small", ui.DANGER, anchor="w", justify="left",
                                   wraplength=440)
        self.badge.set("idle", "Nicht aktiv")

        # --- Zugang ---
        access = ui.card(body)
        access.pack(fill="x", pady=(0, ui.S2))
        form = ctk.CTkFrame(access, fg_color="transparent")
        form.pack(fill="x", **pad)
        names = ctk.CTkFrame(form, fg_color="transparent")
        names.pack(fill="x", pady=(0, ui.S2))
        names.grid_columnconfigure((0, 1), weight=1, uniform="names")
        self.f_alias = ui.Field(names, "Name im Netz (Alias)",
                                args.alias or saved.get("alias") or default_alias(), wrap=210)
        self.f_alias.grid(row=0, column=0, sticky="new", padx=(0, ui.S2))
        self.f_user = ui.Field(names, "Benutzername", args.user or saved.get("user", ""), wrap=210)
        self.f_user.grid(row=0, column=1, sticky="new")

        # Passwort: gespeichert -> eine Zeile mit "Ändern", sonst zwei Felder
        self.pw_saved_row = ctk.CTkFrame(form, fg_color="transparent")
        ui.label(self.pw_saved_row, "✓  Passwort ist gespeichert", "body", ui.SUCCESS
                 ).pack(side="left")
        self.pw_change_btn = ui.secondary_button(self.pw_saved_row, "Ändern",
                                                 self._show_pw_fields, width=80, height=30)
        self.pw_change_btn.pack(side="right")
        self.pw_fields = ctk.CTkFrame(form, fg_color="transparent")
        self.pw_fields.grid_columnconfigure((0, 1), weight=1, uniform="pw")
        self.f_pw = ui.Field(self.pw_fields, "Passwort", secret=True, wrap=210)
        self.f_pw.grid(row=0, column=0, sticky="new", padx=(0, ui.S2))
        self.f_pw2 = ui.Field(self.pw_fields, "Passwort wiederholen", secret=True, wrap=210)
        self.f_pw2.grid(row=0, column=1, sticky="new")
        self.pw_anchor = ctk.CTkFrame(form, fg_color="transparent", height=1)
        self.pw_anchor.pack(fill="x")
        self._update_pw_hint()

        self.remember_var = tk.BooleanVar(value=saved.get("remember", True))
        ui.checkbox(form, "Einstellungen und Zugangsdaten speichern", self.remember_var,
                    self.on_remember_toggle).pack(anchor="w", pady=(ui.S2, ui.S1))
        self.autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        if autostart.supported():
            ui.checkbox(form, "Bei der Anmeldung automatisch starten und freigeben",
                        self.autostart_var, self.on_autostart_toggle).pack(anchor="w",
                                                                           pady=(ui.S1, 0))

        # --- Eingeklappt: Erweitert, Protokoll ---
        folds = ctk.CTkFrame(body, fg_color="transparent")
        folds.pack(fill="both", expand=True)
        advanced = ui.Collapsible(folds, "Erweitert", on_toggle=self.fit_window).pack(fill="x")
        self.advanced = advanced
        self.f_port = ui.Field(advanced.body, "Port",
                               str(args.port or saved.get("port", proto.DEFAULT_PORT)),
                               hint="Standard 8080. Nur ändern, wenn der Port belegt ist.",
                               width=120)
        self.f_port.pack(anchor="w", pady=(ui.S1, 0))
        ui.secondary_button(advanced.body, "Beim Schließen wieder fragen",
                            self._forget_close_action).pack(anchor="w", pady=(ui.S2, ui.S1))
        self.fields = [self.f_alias, self.f_user, self.f_pw, self.f_pw2, self.f_port]

        log_section = ui.Collapsible(folds, "Protokoll", on_toggle=self.fit_window
                                     ).pack(fill="both", expand=True)
        self.logbox = ctk.CTkTextbox(log_section.body, height=130, corner_radius=8,
                                     border_width=1, border_color=ui.BORDER, fg_color=ui.CARD,
                                     text_color=ui.TEXT, font=ui.font("mono"), wrap="word")
        self.logbox.pack(fill="both", expand=True)
        self.logbox.configure(state="disabled")
        self.log_section = log_section
        self.root.after(10, self.fit_window)

        root.bind("<Return>", lambda _e: self.toggle() if self.server is None else None)
        if not self.f_user.get():
            self.f_user.entry.focus_set()
        elif self.saved_creds is None:
            self.f_pw.entry.focus_set()
        root.after(150, self.poll_queues)

        if autostart_launch:
            if self.saved_creds is not None and self._begin(
                    self.saved_creds, proto.clean_alias(saved.get("alias", "")) or default_alias(),
                    int(saved.get("port", proto.DEFAULT_PORT))):
                self.hide_to_background()
            else:
                self.show_error("Keine gespeicherte Freigabe gefunden. Bitte Passwort eingeben "
                                "und Freigabe starten.")

    # ----------------------------- Hilfen -----------------------------
    @staticmethod
    def _stored_creds(saved):
        try:
            return Credentials.from_stored(str(saved["user"]), bytes.fromhex(saved["salt"]),
                                           bytes.fromhex(saved["key"]))
        except (KeyError, ValueError, TypeError):
            return None

    def _update_pw_hint(self):
        """Gespeichertes Passwort: nur eine Zeile mit 'Ändern'. Sonst die zwei Felder."""
        if self.saved_creds is not None and not self.f_pw.get() and not self.f_pw2.get():
            self.pw_fields.pack_forget()
            self.pw_saved_row.pack(fill="x", pady=(ui.S1, ui.S1), before=self.pw_anchor)
        else:
            self.pw_saved_row.pack_forget()
            self.pw_fields.pack(fill="x", before=self.pw_anchor)
        self.fit_window()

    def _show_pw_fields(self):
        self.pw_saved_row.pack_forget()
        self.pw_fields.pack(fill="x", before=self.pw_anchor)
        self.f_pw.set_hint("Leer lassen = gespeichertes Passwort behalten.")
        self.f_pw.entry.focus_set()
        self.fit_window()

    def fit_window(self):
        """Fensterhöhe an den Inhalt anpassen (kein Scrollen nötig)."""
        try:
            self.root.update_idletasks()
            self.root.tk.call("wm", "geometry", self.root._w, "")
        except tk.TclError:
            pass

    def show_error(self, text):
        self.form_error.configure(text=text)
        if text:
            self.form_error.pack(anchor="w", pady=(ui.S2, 0))
        else:
            self.form_error.pack_forget()

    def toggle(self):
        if self.server is None:
            self.start()
        else:
            self.stop()

    def _set_running_ui(self, running, alias="", address=""):
        for field in self.fields:
            field.set_state(not running)
        self.pw_change_btn.configure(state="disabled" if running else "normal")
        if self.back_button is not None:
            self.back_button.configure(state="disabled" if running else "normal")
        self.button.destroy()
        parent = self.status_inner
        if running:
            self.button = ui.danger_button(parent, "Freigabe stoppen", self.toggle)
            self.badge.set("active", "Wartet auf Verbindung")
            self.headline.configure(text=f"„{alias}“")
            self.subline.configure(text=f"Im Netz erreichbar als „{alias}“ oder unter {address}.")
        else:
            self.button = ui.primary_button(parent, "Freigabe starten", self.toggle)
            self.badge.set("idle", "Nicht aktiv")
            self.headline.configure(text="Freigabe ist aus")
            self.subline.configure(text="Lege Zugangsdaten fest und starte die Freigabe.")
        self.button.pack(fill="x", before=self.form_error if self.form_error.winfo_ismapped()
                         else None)

    # ----------------------------- Speichern -----------------------------
    def _save_config(self):
        """Speichert Alias, Benutzer und Port, mit Haken auch Salt + abgeleiteten
        Schlüssel (nie das Passwort). Ohne Haken werden Zugangsdaten entfernt."""
        try:
            port = int(self.f_port.get())
        except ValueError:
            port = proto.DEFAULT_PORT
        data = {"alias": proto.clean_alias(self.f_alias.get()), "user": self.f_user.get().strip(),
                "port": port, "remember": self.remember_var.get()}
        previous = autostart.load_json(HOST_CONFIG)
        if previous.get("close_action"):
            data["close_action"] = previous["close_action"]
        creds = self.creds or self.saved_creds
        if (self.remember_var.get() or self.autostart_var.get()) and creds is not None:
            data.update(user=creds.username, salt=creds.salt.hex(), key=creds.key.hex())
            self.saved_creds = creds
        else:
            self.saved_creds = None
        try:
            autostart.save_json(HOST_CONFIG, data, private=True)
        except OSError as exc:
            log(f"[!] Einstellungen konnten nicht gespeichert werden: {exc}")
        self._update_pw_hint()

    def on_remember_toggle(self):
        self._save_config()
        if not self.remember_var.get() and not self.autostart_var.get():
            log("[*] Gespeicherte Zugangsdaten gelöscht")

    def on_autostart_toggle(self):
        try:
            if self.autostart_var.get():
                if self.creds is None and self.saved_creds is None:
                    log("[*] Autostart wird eingerichtet, sobald du die Freigabe startest")
                    return
                self._save_config()
                autostart.enable()
                log("[*] Autostart aktiv: HopGrid gibt nach der Anmeldung automatisch frei")
            else:
                autostart.disable()
                self._save_config()
                log("[*] Autostart aus")
        except OSError as exc:
            self.show_error(f"Autostart konnte nicht geändert werden: {exc}")

    # ----------------------------- Freigabe -----------------------------
    def start(self):
        self.show_error("")
        alias = proto.clean_alias(self.f_alias.get())
        username = self.f_user.get().strip()
        password, repeat = self.f_pw.get(), self.f_pw2.get()
        try:
            port = int(self.f_port.get().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            if not self.advanced.is_open:
                self.advanced.toggle()
            self.f_port.error("Bitte eine Zahl von 1 bis 65535 eingeben.")
            return
        if not alias:
            self.f_alias.error("Bitte einen Namen eingeben, z. B. „Büro-PC“.")
            return
        if not username:
            self.f_user.error("Bitte einen Benutzernamen festlegen.")
            return

        if not password and not repeat and self.saved_creds is not None:
            if username != self.saved_creds.username:
                self.f_pw.error("Neuer Benutzername: bitte auch ein Passwort festlegen.")
                return
            creds = self.saved_creds
        else:
            if len(password) < MIN_PASSWORD_LEN:
                self.f_pw.error(f"Mindestens {MIN_PASSWORD_LEN} Zeichen.")
                return
            if password != repeat:
                self.f_pw2.error("Stimmt nicht mit dem Passwort überein.")
                return
            creds = Credentials(username, password)
        del password, repeat
        self.f_pw.set("")
        self.f_pw2.set("")
        self._begin(creds, alias, port)

    def _begin(self, creds, alias, port):
        self.args.port = port
        self.args.alias = alias
        try:
            monitor = prepare(self.args)
        except ValueError as exc:
            self.show_error(str(exc))
            return False
        try:
            self.server = open_server(self.args)
        except OSError as exc:
            self.show_error(f"Port {port} ist nicht verfügbar ({exc.strerror or exc}). "
                            "Unter „Erweitert“ einen anderen Port wählen.")
            return False

        self.creds = creds
        self.stop_event = threading.Event()
        threading.Thread(target=serve, args=(self.server, creds, self.args, monitor,
                                             self.stop_event), daemon=True).start()
        self.f_alias.set(alias)
        self.f_user.set(creds.username)
        self.f_port.set(str(port))
        address = f"{local_ip()}:{port}"
        self._set_running_ui(True, alias, address)
        log(f"[*] Freigabe gestartet: {address}, Monitor {self.args.monitor} "
            f"({monitor['width']}x{monitor['height']})")
        self.discovery = start_discovery(self.args)
        self.keep_awake.start()
        self._save_config()
        if self.autostart_var.get():
            try:
                autostart.enable()
            except OSError as exc:
                log(f"[!] Autostart konnte nicht eingerichtet werden: {exc}")
        hints = check_macos_permissions()
        for hint in hints:
            log(f"[!] {hint}")
        if hints:
            self.show_error("macOS-Freigaben fehlen – Details im Protokoll.")
            if not self.log_section.is_open:
                self.log_section.toggle()
        return True

    def stop(self):
        self.stop_event.set()
        if self.discovery:
            self.discovery.stop()
            self.discovery = None
        self.server.close()
        self.server = None
        self.creds = None
        close_active_connections()
        self.keep_awake.stop()
        self.show_error("")
        self._set_running_ui(False)
        log("[*] Freigabe gestoppt")

    # ----------------------------- Navigation / Beenden -----------------------------
    def go_back(self):
        if self.server is None and self.on_back:
            self.on_back()

    def destroy(self):
        self.alive = False
        set_log_handler(print)
        set_status_handler(None)
        if self.server is not None:
            self.stop()
        self.frame.destroy()

    def hide_to_background(self):
        """Fenster ausblenden, Freigabe läuft weiter."""
        if sys.platform == "win32":
            try:
                if self.tray is None:
                    self.tray = TrayIcon(lambda: self.ui_queue.put(("open",)),
                                         lambda: self.ui_queue.put(("quit",)))
            except Exception as exc:
                log(f"[!] Symbol im Infobereich nicht möglich ({exc}), Fenster wird minimiert")
                self.root.iconify()
                return
            self.root.withdraw()
        elif sys.platform == "darwin":
            self.root.withdraw()  # Klick auf das Dock-Symbol holt das Fenster zurück
        else:
            self.root.iconify()

    def show_window(self, *_args):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        choice = autostart.load_json(HOST_CONFIG).get("close_action")
        if choice == "background":
            self.hide_to_background()
            return
        if choice == "quit":
            self.quit_app()
            return
        self.show_window()
        CloseDialog(self.root, sharing=self.server is not None,
                    on_background=self.hide_to_background, on_quit=self.quit_app,
                    on_remember=self._remember_close_action)

    def _forget_close_action(self):
        data = autostart.load_json(HOST_CONFIG)
        if data.pop("close_action", None):
            autostart.save_json(HOST_CONFIG, data, private=True)
        log("[*] Beim Schließen wird wieder gefragt")

    def _remember_close_action(self, action):
        data = autostart.load_json(HOST_CONFIG)
        data["close_action"] = action
        try:
            autostart.save_json(HOST_CONFIG, data, private=True)
            log("[*] Auswahl beim Schließen gemerkt (änderbar unter „Erweitert“)")
        except OSError:
            pass

    def quit_app(self):
        if self.server is not None:
            self.stop()
        if self.tray is not None:
            self.tray.stop()
        self.root.destroy()

    def poll_queues(self):
        if not self.alive:
            return
        while True:
            try:
                item = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if item[0] == "open":
                self.show_window()
            elif item[0] == "quit":
                self.quit_app()
                return
            elif item[0] == "status" and self.server is not None:
                _, event, ip = item
                if event == "connected":
                    self.badge.set("busy", f"Verbunden: {ip}")
                else:
                    self.badge.set("active", "Wartet auf Verbindung")
        lines = []
        while True:
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        if lines:
            stamp = time.strftime("%H:%M:%S")
            self.logbox.configure(state="normal")
            for line in lines:
                self.logbox.insert("end", f"{stamp}  {str(line).strip()}\n")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.root.after(150, self.poll_queues)


class CloseDialog:
    """Frage beim Schließen: im Hintergrund weiterlaufen oder ganz beenden."""

    def __init__(self, root, sharing, on_background, on_quit, on_remember):
        self.on_remember = on_remember
        self.remember_var = tk.BooleanVar(value=False)
        self.top = top = ctk.CTkToplevel(root)
        top.title("HopGrid")
        top.configure(fg_color=ui.BG)
        top.resizable(False, False)
        top.transient(root)
        where = {"win32": "Symbol unten rechts im Infobereich",
                 "darwin": "HopGrid bleibt im Dock"}.get(sys.platform, "minimiert")
        box = ctk.CTkFrame(top, fg_color="transparent")
        box.pack(padx=ui.S4, pady=ui.S4)
        if sharing:
            title = "Die Freigabe läuft noch"
            text = "Soll HopGrid im Hintergrund weiterlaufen? Dann bleibt dieser Rechner erreichbar."
            quit_text = "Freigabe stoppen und beenden"
        else:
            title = "HopGrid schließen?"
            text = "Im Hintergrund bleibt HopGrid griffbereit und startet schneller."
            quit_text = "Ganz beenden"
        ui.label(box, title, "h2").pack(anchor="w")
        ui.label(box, text, "body", ui.MUTED, justify="left", wraplength=360
                 ).pack(anchor="w", pady=(ui.S1, ui.S3))
        ui.primary_button(box, "Im Hintergrund weiterlaufen",
                          lambda: self._choose("background", on_background)).pack(fill="x")
        ui.label(box, where, "small", ui.MUTED).pack(anchor="w", pady=(2, ui.S2))
        ui.danger_button(box, quit_text,
                         lambda: self._choose("quit", on_quit)).pack(fill="x", pady=(0, ui.S2))
        ui.secondary_button(box, "Abbrechen", top.destroy).pack(fill="x")
        ui.checkbox(box, "Auswahl merken und nicht mehr fragen", self.remember_var
                    ).pack(anchor="w", pady=(ui.S3, 0))
        top.protocol("WM_DELETE_WINDOW", top.destroy)
        top.bind("<Escape>", lambda _e: top.destroy())
        top.after(50, self._focus)

    def _focus(self):
        try:
            self.top.grab_set()
            self.top.focus_force()
        except tk.TclError:
            pass

    def _choose(self, name, action):
        if self.remember_var.get():
            self.on_remember(name)
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        self.top.destroy()
        action()


def run_gui(args):
    ui.setup()
    root = ctk.CTk()
    root.configure(fg_color=ui.BG)
    root.minsize(540, 300)
    ui.set_window_icon(root)
    HostWindow(root, args, autostart_launch=getattr(args, "autostart", False))
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description="HopGrid Host – Bildschirm im LAN teilen")
    parser.add_argument("--cli", action="store_true", help="Terminal statt Fenster")
    parser.add_argument("--bind", default="0.0.0.0", help="Adresse zum Lauschen (Standard: alle)")
    parser.add_argument("--port", type=int, help=f"Port (Standard {proto.DEFAULT_PORT})")
    parser.add_argument("--alias", help="Name, unter dem der Host im LAN gefunden wird "
                                         "(Standard: Rechnername)")
    parser.add_argument("--user", help="Benutzername (vorausgefüllt)")
    parser.add_argument("--fps", type=int, default=15, help="Bilder pro Sekunde (1-60)")
    parser.add_argument("--quality", type=int, default=85, help="JPEG-Qualität (10-95)")
    parser.add_argument("--max-width", type=int, default=2560,
                        help="Bild wird auf diese Breite verkleinert (spart Bandbreite)")
    parser.add_argument("--monitor", type=int, default=1, help="Monitor-Nummer (1 = Hauptmonitor)")
    parser.add_argument("--allow-public", action="store_true",
                        help="Auch Verbindungen von nicht-privaten IPs erlauben (nicht empfohlen)")
    # parse_known_args: macOS kann beim App-Start eigene Argumente (-psn_…) anhängen
    args, _unknown = parser.parse_known_args()

    if args.cli:
        args.port = args.port or proto.DEFAULT_PORT
        run_cli(args)
    else:
        run_gui(args)


if __name__ == "__main__":
    main()

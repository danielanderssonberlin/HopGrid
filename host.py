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
from tkinter import scrolledtext


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
def stream_frames(channel, stop, args):
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
            except Exception as exc:
                if not capture_failed:
                    log(f"[!] Bildschirm gerade nicht lesbar ({exc}). Verbindung bleibt, "
                        "neuer Versuch läuft.")
                    capture_failed = True
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


def run_session(channel, ip, args, monitor):
    stop = threading.Event()
    executor = InputExecutor(monitor)
    sender = threading.Thread(target=stream_frames, args=(channel, stop, args), daemon=True)
    sender.start()
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
            run_session(channel, ip, args, monitor)
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
def resource_path(relative):
    """Pfad zu mitgelieferten Dateien, auch in der gebauten App."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


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
    """Fenster für die Freigabe. Mit autostart_launch=True startet es sofort mit den
    gespeicherten Zugangsdaten und läuft im Hintergrund."""

    def __init__(self, root, args, autostart_launch=False):
        self.root = root
        self.args = args
        self.server = None
        self.stop_event = None
        self.discovery = None
        self.creds = None
        self.tray = None
        self.keep_awake = KeepAwake()
        self.log_queue = queue.Queue()
        self.ui_queue = queue.Queue()   # Befehle aus dem Infobereich-Thread
        set_log_handler(self.log_queue.put)

        saved = autostart.load_json(HOST_CONFIG)
        self.saved_creds = self._stored_creds(saved)

        root.title("HopGrid – Freigabe")
        root.minsize(500, 460)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        if sys.platform == "darwin":
            root.createcommand("::tk::mac::Quit", self.on_close)
            root.createcommand("::tk::mac::ReopenApplication", self.show_window)

        form = tk.Frame(root, padx=16, pady=12)
        form.pack(fill="x")
        rows = [("Alias (Name im Netz)", args.alias or saved.get("alias") or default_alias(), ""),
                ("Benutzername", args.user or saved.get("user", ""), ""),
                ("Passwort", "", "•"),
                ("Passwort wiederholen", "", "•"),
                ("Port", str(args.port or saved.get("port", proto.DEFAULT_PORT)), "")]
        self.entries = []
        for row, (label, value, show) in enumerate(rows):
            tk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 10))
            entry = tk.Entry(form, width=28, show=show)
            entry.insert(0, value)
            entry.grid(row=row, column=1, sticky="we", pady=3)
            self.entries.append(entry)
        form.columnconfigure(1, weight=1)

        self.pw_hint = tk.Label(form, fg="#555", text="")
        self.pw_hint.grid(row=5, column=1, sticky="w")
        self._update_pw_hint()

        self.remember_var = tk.BooleanVar(value=saved.get("remember", True))
        tk.Checkbutton(form, text="Einstellungen und Zugangsdaten speichern",
                       variable=self.remember_var, command=self.on_remember_toggle
                       ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        if autostart.supported():
            tk.Checkbutton(form, text="Bei der Anmeldung automatisch starten und freigeben",
                           variable=self.autostart_var, command=self.on_autostart_toggle
                           ).grid(row=7, column=0, columnspan=2, sticky="w")

        self.button = tk.Button(form, text="Freigabe starten", command=self.toggle)
        self.button.grid(row=8, column=0, columnspan=2, pady=(10, 2))

        self.status = tk.Label(root, text="Nicht aktiv", font=("TkDefaultFont", 13, "bold"))
        self.status.pack(pady=(0, 6))

        self.logbox = scrolledtext.ScrolledText(root, height=9, width=60, state="disabled")
        self.logbox.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        root.bind("<Return>", lambda _e: self.toggle() if self.server is None else None)
        (self.entries[2] if self.entries[1].get() else self.entries[1]).focus_set()
        root.after(150, self.poll_queues)

        if autostart_launch:
            if self.saved_creds is not None and self._begin(
                    self.saved_creds, proto.clean_alias(saved.get("alias", "")) or default_alias(),
                    int(saved.get("port", proto.DEFAULT_PORT))):
                self.hide_to_background()
            else:
                log("[!] Keine gespeicherte Freigabe gefunden. Bitte Passwort eingeben und "
                    "Freigabe starten.")

    # ----------------------------- Hilfen -----------------------------
    @staticmethod
    def _stored_creds(saved):
        try:
            return Credentials.from_stored(str(saved["user"]), bytes.fromhex(saved["salt"]),
                                           bytes.fromhex(saved["key"]))
        except (KeyError, ValueError, TypeError):
            return None

    def _update_pw_hint(self):
        text = ""
        if self.saved_creds is not None:
            text = "Passwort ist gespeichert – Felder leer lassen, um es weiter zu nutzen."
        self.pw_hint.config(text=text)

    def set_status(self, text, color="black"):
        self.status.config(text=text, fg=color)

    def toggle(self):
        if self.server is None:
            self.start()
        else:
            self.stop()

    # ----------------------------- Speichern -----------------------------
    def _save_config(self):
        """Speichert Alias, Benutzer und Port, mit Haken auch Salt + abgeleiteten
        Schlüssel (nie das Passwort). Ohne Haken werden Zugangsdaten entfernt."""
        data = {
            "alias": proto.clean_alias(self.entries[0].get()),
            "user": self.entries[1].get().strip(),
            "port": self.args.port or proto.DEFAULT_PORT,
            "remember": self.remember_var.get(),
        }
        creds = self.creds or self.saved_creds
        keep_secret = self.remember_var.get() or self.autostart_var.get()
        if keep_secret and creds is not None:
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
            log(f"[!] Autostart konnte nicht geändert werden: {exc}")

    # ----------------------------- Freigabe -----------------------------
    def start(self):
        alias = proto.clean_alias(self.entries[0].get())
        username = self.entries[1].get().strip()
        password = self.entries[2].get()
        repeat = self.entries[3].get()
        if not alias:
            self.set_status("Bitte einen Alias eingeben.", "#b00020")
            return
        if not username:
            self.set_status("Bitte Benutzernamen eingeben.", "#b00020")
            return
        try:
            port = int(self.entries[4].get().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            self.set_status("Port muss eine Zahl von 1 bis 65535 sein.", "#b00020")
            return

        if not password and not repeat and self.saved_creds is not None:
            if username != self.saved_creds.username:
                self.set_status("Neuer Benutzername: bitte auch ein Passwort eingeben.",
                                "#b00020")
                return
            creds = self.saved_creds
        else:
            if len(password) < MIN_PASSWORD_LEN:
                self.set_status(f"Passwort: mindestens {MIN_PASSWORD_LEN} Zeichen.", "#b00020")
                return
            if password != repeat:
                self.set_status("Passwörter stimmen nicht überein.", "#b00020")
                return
            creds = Credentials(username, password)
        del password, repeat
        for entry in self.entries[2:4]:
            entry.delete(0, "end")
        self._begin(creds, alias, port)

    def _begin(self, creds, alias, port):
        self.args.port = port
        self.args.alias = alias
        try:
            monitor = prepare(self.args)
        except ValueError as exc:
            self.set_status(str(exc), "#b00020")
            return False
        try:
            self.server = open_server(self.args)
        except OSError as exc:
            self.set_status(f"Port {port} nicht verfügbar: {exc.strerror or exc}", "#b00020")
            return False

        self.creds = creds
        self.stop_event = threading.Event()
        threading.Thread(target=serve, args=(self.server, creds, self.args, monitor,
                                             self.stop_event), daemon=True).start()
        for i, value in ((0, alias), (1, creds.username), (4, str(port))):
            self.entries[i].delete(0, "end")
            self.entries[i].insert(0, value)
        for entry in self.entries:
            entry.config(state="disabled")
        self.button.config(text="Freigabe stoppen")
        address = f"{local_ip()}:{port}"
        self.set_status(f"Aktiv – Alias \"{alias}\" oder {address}", "#1b7f3b")
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
        for hint in check_macos_permissions():
            log(f"[!] {hint}")
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
        for entry in self.entries:
            entry.config(state="normal")
        self.button.config(text="Freigabe starten")
        self.set_status("Nicht aktiv")
        log("[*] Freigabe gestoppt")
        self.entries[2].focus_set()

    # ----------------------------- Hintergrund / Beenden -----------------------------
    def hide_to_background(self):
        """Fenster ausblenden, Freigabe läuft weiter."""
        if sys.platform == "win32":
            try:
                if self.tray is None:
                    self.tray = TrayIcon(lambda: self.ui_queue.put("open"),
                                         lambda: self.ui_queue.put("quit"))
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
        if self.server is None:
            self.quit_app()
            return
        self.show_window()
        CloseDialog(self.root, on_background=self.hide_to_background, on_quit=self.quit_app)

    def quit_app(self):
        if self.server is not None:
            self.stop()
        if self.tray is not None:
            self.tray.stop()
        self.root.destroy()

    def poll_queues(self):
        while True:
            try:
                command = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if command == "open":
                self.show_window()
            elif command == "quit":
                self.quit_app()
                return
        lines = []
        while True:
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        if lines:
            stamp = time.strftime("%H:%M:%S")
            self.logbox.config(state="normal")
            for line in lines:
                self.logbox.insert("end", f"{stamp}  {str(line).strip()}\n")
            self.logbox.see("end")
            self.logbox.config(state="disabled")
        self.root.after(150, self.poll_queues)


class CloseDialog:
    """Frage beim Schließen: im Hintergrund weiterlaufen oder ganz beenden."""

    def __init__(self, root, on_background, on_quit):
        self.top = top = tk.Toplevel(root)
        top.title("HopGrid")
        top.transient(root)
        top.resizable(False, False)
        where = {"win32": "Symbol unten rechts im Infobereich",
                 "darwin": "HopGrid bleibt im Dock"}.get(sys.platform, "minimiert")
        tk.Label(top, text="Die Freigabe ist aktiv.", font=("TkDefaultFont", 13, "bold"),
                 padx=20).pack(pady=(16, 4))
        tk.Label(top, text="Soll HopGrid im Hintergrund weiterlaufen oder ganz beendet werden?",
                 padx=20, wraplength=340).pack()
        buttons = tk.Frame(top, padx=16, pady=14)
        buttons.pack()
        tk.Button(buttons, text=f"Im Hintergrund weiterlaufen\n({where})",
                  command=lambda: self._choose(on_background)).pack(fill="x", pady=3)
        tk.Button(buttons, text="Ganz beenden (Freigabe stoppen)",
                  command=lambda: self._choose(on_quit)).pack(fill="x", pady=3)
        tk.Button(buttons, text="Abbrechen", command=top.destroy).pack(fill="x", pady=3)
        top.protocol("WM_DELETE_WINDOW", top.destroy)
        top.grab_set()
        top.focus_force()

    def _choose(self, action):
        self.top.grab_release()
        self.top.destroy()
        action()


def run_gui(args):
    root = tk.Tk()
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

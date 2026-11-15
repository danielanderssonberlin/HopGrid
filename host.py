#!/usr/bin/env python3
"""
HopGrid Host – teilt den Bildschirm im lokalen Netz und führt Maus-/Tastatureingaben
eines verbundenen Clients aus. Kein externer Server, direkte, verschlüsselte TCP-Verbindung.

Start:
    python host.py                       # Fenster (Standard)
    python host.py --cli                 # Terminal-Modus, fragt Benutzername/Passwort ab
    python host.py --port 8080 --fps 15 --quality 70 --max-width 1600 --monitor 1
"""

import argparse
import getpass
import hmac
import ipaddress
import json
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import scrolledtext


def enable_dpi_awareness():
    """Windows: Prozess DPI-aware machen, damit Bildschirmaufnahme und Mauskoordinaten
    beide in physischen Pixeln rechnen (sonst Versatz bei 125 %/150 % Skalierung).
    Muss vor dem Import von mss/pynput passieren."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor-DPI-aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


enable_dpi_awareness()

import cv2  # noqa: E402
import mss  # noqa: E402
import numpy as np  # noqa: E402
from pynput.keyboard import Controller as KeyboardController  # noqa: E402
from pynput.keyboard import Key, KeyCode  # noqa: E402
from pynput.mouse import Button  # noqa: E402
from pynput.mouse import Controller as MouseController  # noqa: E402

import protocol as proto  # noqa: E402

if sys.platform == "darwin":
    import Quartz  # wird mit pynput (pyobjc) installiert

# mss >= 10 heißt die Klasse MSS, ältere Versionen nutzen mss.mss
MSS = getattr(mss, "MSS", None) or mss.mss

MAX_INPUT_MSG = 64 * 1024    # Eingaben sind klein; schützt vor Speichermissbrauch
AUTH_TIMEOUT = 15            # Sekunden für den kompletten Login
FAIL_DELAY = 2.0             # Verzögerung nach falschem Login (bremst Raten)
MAX_FAILS = 5                # Fehlversuche pro IP bis zur Sperre
BLOCK_SECONDS = 300          # Dauer der IP-Sperre
KEEPALIVE_SECONDS = 2.0      # spätestens so oft ein Bild senden, auch ohne Änderung
MIN_PASSWORD_LEN = 8


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
    """Läuft in eigenem Thread. mss-Instanz muss im selben Thread erzeugt werden."""
    interval = 1.0 / args.fps
    encode_params = [cv2.IMWRITE_JPEG_QUALITY, args.quality]
    try:
        with MSS() as sct:
            monitor = sct.monitors[args.monitor]
            last_img = None
            last_sent = 0.0
            while not stop.is_set():
                started = time.monotonic()

                shot = sct.grab(monitor)                       # BGRA, physische Pixel
                img = cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2BGR)

                h, w = img.shape[:2]
                if w > args.max_width:                         # Bandbreite sparen
                    new_h = int(h * args.max_width / w)
                    img = cv2.resize(img, (args.max_width, new_h), interpolation=cv2.INTER_AREA)

                changed = last_img is None or not np.array_equal(img, last_img)
                if changed or started - last_sent >= KEEPALIVE_SECONDS:
                    ok, jpeg = cv2.imencode(".jpg", img, encode_params)
                    if ok:
                        channel.send(proto.MSG_FRAME, jpeg.tobytes())
                        last_img = img
                        last_sent = started

                stop.wait(max(0.0, interval - (time.monotonic() - started)))
    except (OSError, ConnectionError) as exc:
        if not stop.is_set():
            log(f"[-] Senden abgebrochen: {exc}")
    except Exception as exc:  # z. B. fehlende Bildschirmfreigabe
        log(f"[!] Fehler bei der Bildschirmaufnahme: {exc}")
    finally:
        stop.set()
        try:
            channel.sock.shutdown(socket.SHUT_RDWR)  # weckt den Empfangs-Thread auf
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Verbindung eines Clients
# ---------------------------------------------------------------------------
def run_session(channel, ip, args, monitor):
    stop = threading.Event()
    executor = InputExecutor(monitor)
    sender = threading.Thread(target=stream_frames, args=(channel, stop, args), daemon=True)
    sender.start()
    try:
        while not stop.is_set():
            msg_type, payload = channel.recv()
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
            except (KeyError, TypeError, ValueError) as exc:
                log(f"[!] Ungültiges Ereignis von {ip} ignoriert: {exc}")
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
        if not session_lock.acquire(blocking=False):
            proto.send_plain(conn, proto.MSG_AUTH_RESULT,
                             {"ok": False, "reason": "Es ist bereits ein Client verbunden."})
            return
        try:
            # 3. Gegenbeweis senden, ab jetzt verschlüsselt
            host_to_client, client_to_host = kx.session_keys(client_pub, creds.key, transcript)
            proto.send_plain(conn, proto.MSG_AUTH_RESULT, {
                "ok": True,
                "proof": proto.make_proof(creds.key, b"host", transcript).hex(),
            })
            channel = proto.SecureChannel(conn, host_to_client, client_to_host, MAX_INPUT_MSG)
            conn.settimeout(None)
            log(f"[+] {ip} verbunden (verschlüsselt)")
            run_session(channel, ip, args, monitor)
        finally:
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


def is_local_address(ip):
    addr = ipaddress.ip_address(ip)
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return addr.is_private or addr.is_loopback or addr.is_link_local


def local_ip():
    """Ermittelt die LAN-IP. UDP-connect sendet kein Paket, es wählt nur die Route."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


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

    server = open_server(args)
    log(f"[*] HopGrid Host läuft. Verbinden mit {local_ip()}:{args.port}")
    log(f"[*] Monitor {args.monitor}: {monitor['width']}x{monitor['height']}, "
        f"{args.fps} FPS, JPEG {args.quality}. Beenden mit Strg+C.")
    for hint in check_macos_permissions():
        log(f"[!] {hint}")
    try:
        serve(server, creds, args, monitor, threading.Event())
    except KeyboardInterrupt:
        log("\n[*] Beendet.")
    finally:
        server.close()


# ---------------------------------------------------------------------------
# Fenster-Modus
# ---------------------------------------------------------------------------
class HostWindow:
    def __init__(self, root, args):
        self.root = root
        self.args = args
        self.server = None
        self.stop_event = None
        self.log_queue = queue.Queue()
        set_log_handler(self.log_queue.put)

        root.title("HopGrid Host")
        root.minsize(460, 380)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        form = tk.Frame(root, padx=16, pady=12)
        form.pack(fill="x")
        rows = [("Benutzername", args.user or "", ""),
                ("Passwort", "", "•"),
                ("Passwort wiederholen", "", "•"),
                ("Port", str(args.port), "")]
        self.entries = []
        for row, (label, value, show) in enumerate(rows):
            tk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 10))
            entry = tk.Entry(form, width=28, show=show)
            entry.insert(0, value)
            entry.grid(row=row, column=1, sticky="we", pady=3)
            self.entries.append(entry)
        form.columnconfigure(1, weight=1)

        self.button = tk.Button(form, text="Freigabe starten", command=self.toggle)
        self.button.grid(row=4, column=0, columnspan=2, pady=(10, 2))

        self.status = tk.Label(root, text="Nicht aktiv", font=("TkDefaultFont", 13, "bold"))
        self.status.pack(pady=(0, 6))

        self.logbox = scrolledtext.ScrolledText(root, height=9, width=60, state="disabled")
        self.logbox.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        root.bind("<Return>", lambda _e: self.toggle() if self.server is None else None)
        (self.entries[1] if args.user else self.entries[0]).focus_set()
        root.after(150, self.poll_log)

    def set_status(self, text, color="black"):
        self.status.config(text=text, fg=color)

    def toggle(self):
        if self.server is None:
            self.start()
        else:
            self.stop()

    def start(self):
        username = self.entries[0].get().strip()
        password = self.entries[1].get()
        repeat = self.entries[2].get()
        if not username:
            self.set_status("Bitte Benutzernamen eingeben.", "#b00020")
            return
        if len(password) < MIN_PASSWORD_LEN:
            self.set_status(f"Passwort: mindestens {MIN_PASSWORD_LEN} Zeichen.", "#b00020")
            return
        if password != repeat:
            self.set_status("Passwörter stimmen nicht überein.", "#b00020")
            return
        try:
            port = int(self.entries[3].get().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            self.set_status("Port muss eine Zahl von 1 bis 65535 sein.", "#b00020")
            return

        self.args.port = port
        try:
            monitor = prepare(self.args)
        except ValueError as exc:
            self.set_status(str(exc), "#b00020")
            return
        creds = Credentials(username, password)
        del password, repeat
        for entry in self.entries[1:3]:
            entry.delete(0, "end")

        try:
            self.server = open_server(self.args)
        except OSError as exc:
            self.set_status(f"Port {port} nicht verfügbar: {exc.strerror or exc}", "#b00020")
            return

        self.stop_event = threading.Event()
        threading.Thread(target=serve, args=(self.server, creds, self.args, monitor,
                                             self.stop_event), daemon=True).start()
        for entry in self.entries:
            entry.config(state="disabled")
        self.button.config(text="Freigabe stoppen")
        address = f"{local_ip()}:{port}"
        self.set_status(f"Aktiv – verbinden mit {address}", "#1b7f3b")
        log(f"[*] Freigabe gestartet: {address}, Monitor {self.args.monitor} "
            f"({monitor['width']}x{monitor['height']})")
        for hint in check_macos_permissions():
            log(f"[!] {hint}")

    def stop(self):
        self.stop_event.set()
        self.server.close()
        self.server = None
        close_active_connections()
        for entry in self.entries:
            entry.config(state="normal")
        self.button.config(text="Freigabe starten")
        self.set_status("Nicht aktiv")
        log("[*] Freigabe gestoppt")
        self.entries[1].focus_set()

    def poll_log(self):
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
        self.root.after(150, self.poll_log)

    def on_close(self):
        if self.server is not None:
            self.stop()
        self.root.destroy()


def run_gui(args):
    root = tk.Tk()
    HostWindow(root, args)
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description="HopGrid Host – Bildschirm im LAN teilen")
    parser.add_argument("--cli", action="store_true", help="Terminal statt Fenster")
    parser.add_argument("--bind", default="0.0.0.0", help="Adresse zum Lauschen (Standard: alle)")
    parser.add_argument("--port", type=int, default=proto.DEFAULT_PORT)
    parser.add_argument("--user", help="Benutzername (vorausgefüllt)")
    parser.add_argument("--fps", type=int, default=15, help="Bilder pro Sekunde (1-60)")
    parser.add_argument("--quality", type=int, default=70, help="JPEG-Qualität (10-95)")
    parser.add_argument("--max-width", type=int, default=1600,
                        help="Bild wird auf diese Breite verkleinert (spart Bandbreite)")
    parser.add_argument("--monitor", type=int, default=1, help="Monitor-Nummer (1 = Hauptmonitor)")
    parser.add_argument("--allow-public", action="store_true",
                        help="Auch Verbindungen von nicht-privaten IPs erlauben (nicht empfohlen)")
    # parse_known_args: macOS kann beim App-Start eigene Argumente (-psn_…) anhängen
    args, _unknown = parser.parse_known_args()

    if args.cli:
        run_cli(args)
    else:
        run_gui(args)


if __name__ == "__main__":
    main()

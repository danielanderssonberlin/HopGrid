#!/usr/bin/env python3
"""
HopGrid Client – verbindet sich mit einem HopGrid Host im lokalen Netz, zeigt dessen
Bildschirm an und überträgt Maus- und Tastatureingaben. Alles nach dem Login
ist Ende-zu-Ende verschlüsselt (siehe protocol.py).

Start:
    python client.py
    python client.py --host 192.168.1.20 --port 8080 --user daniel
    python client.py --cmd-as-ctrl      # Mac-Client steuert Windows: Cmd wird zu Strg
"""

import argparse
import errno
import hmac
import io
import ipaddress
import json
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from PIL import Image, ImageTk

import autostart
import filetransfer
import passwords
import ui
import protocol as proto
from dpi import enable_dpi_awareness

MAX_FRAME = 50 * 1024 * 1024
CONNECT_TIMEOUT = 5
AUTH_TIMEOUT = 15
STREAM_TIMEOUT = 10        # Host sendet spätestens alle 2 s ein Bild
MOVE_INTERVAL_MS = 12      # Mausbewegungen bündeln (ca. 80 pro Sekunde)
RENDER_INTERVAL_MS = 15
PING_INTERVAL_MS = 2000      # Lebenszeichen an den Host, wenn sonst nichts gesendet wird
RECONNECT_SECONDS = 120      # so lange wird nach einem Abbruch neu verbunden
RECONNECT_PAUSE = 2.0
CLIENT_CONFIG = "client.json"   # gemerkte Eingaben (ohne Passwort)


class AuthError(Exception):
    """Login abgelehnt (falsches Passwort, gefälschter Host). Neu versuchen ist sinnlos."""


class HostNotFound(ConnectionError):
    """Alias im Netz nicht gefunden. Kann sich ändern, z. B. wenn der Host neu startet."""


# ---------------------------------------------------------------------------
# Gemerkte Eingaben (ohne Passwort)
# ---------------------------------------------------------------------------
def load_settings():
    return autostart.load_json(CLIENT_CONFIG)


def save_settings(data):
    try:
        autostart.save_json(CLIENT_CONFIG, data)
    except OSError:
        pass  # Merken ist nur Komfort


def resolve_target(target, port):
    """IP direkt verwenden, sonst Alias im LAN suchen, zuletzt als Rechnername auflösen.
    Liefert (ip, port)."""
    try:
        ipaddress.ip_address(target)
        return target, port
    except ValueError:
        pass
    for found in proto.discover():
        if found["alias"].casefold() == target.casefold():
            return found["ip"], found["port"]
    try:
        return socket.gethostbyname(target), port  # z. B. "macbook.local"
    except OSError:
        raise HostNotFound(f"„{target}“ nicht gefunden. Läuft der Host mit diesem Alias "
                        "im selben Netz und ist die Freigabe gestartet?") from None


# "Nicht erreichbar"-Fehler, die beim ersten Versuch oft vorübergehend sind:
# Zielrechner muss erst per ARP antworten (Energiesparmodus), oder macOS gibt die
# Freigabe "Lokales Netzwerk" erst nach dem ersten Versuch frei.
TRANSIENT_ERRORS = {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN,
                    10065, 10051, 10060}  # Windows: WSAEHOSTUNREACH, WSAENETUNREACH, WSAETIMEDOUT
CONNECT_ATTEMPTS = 4


def open_connection(host, port):
    """TCP-Verbindung mit kurzen Wiederholungen bei vorübergehenden Netzfehlern."""
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            return socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        except OSError as exc:
            code = getattr(exc, "winerror", None) or exc.errno
            transient = code in TRANSIENT_ERRORS or isinstance(exc, socket.timeout)
            if not transient or attempt == CONNECT_ATTEMPTS:
                raise
            time.sleep(0.6 * attempt)


def friendly_error(exc):
    """Technische Netzwerkfehler in verständliche Sätze übersetzen."""
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    if isinstance(exc, ConnectionRefusedError) or code in (errno.ECONNREFUSED, 10061):
        return ("Der Rechner ist erreichbar, aber dort läuft keine Freigabe. "
                "Bitte auf dem anderen Rechner „Freigabe starten“ klicken.")
    if isinstance(exc, socket.timeout) or code in TRANSIENT_ERRORS:
        return ("Der Rechner ist nicht erreichbar. Ist er eingeschaltet, im selben Netz und "
                "lässt seine Firewall HopGrid zu?")
    return f"Verbindung fehlgeschlagen: {exc}"


def connect_and_authenticate(host, port, username, password):
    """Baut die Verbindung auf, meldet sich an und liefert einen verschlüsselten Kanal.

    Das Passwort selbst wird nie gesendet. Der Host muss seinerseits beweisen, dass er
    das Passwort kennt, sonst bricht der Client ab (Schutz vor gefälschtem Host)."""
    sock = open_connection(host, port)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(AUTH_TIMEOUT)

        challenge = proto.recv_plain(sock, proto.MSG_CHALLENGE)
        if challenge.get("v") != proto.PROTOCOL_VERSION:
            raise AuthError("Der Host hat eine andere HopGrid-Version. Bitte beide Rechner "
                            "auf die neueste Version aktualisieren.")
        salt = proto.hex_field(challenge, "salt", 16)
        nonce = proto.hex_field(challenge, "nonce", 32)
        host_pub = proto.hex_field(challenge, "pub", 32)
        try:
            iterations = int(challenge["iterations"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthError("Ungültige Antwort vom Host.") from exc
        if not proto.MIN_ITERATIONS <= iterations <= proto.MAX_ITERATIONS:
            raise AuthError("Host verlangt eine ungültige Iterationszahl.")

        password_key = proto.derive_password_key(password, salt, iterations)
        kx = proto.KeyExchange()
        transcript = proto.build_transcript(nonce, host_pub, kx.public, username)
        proto.send_plain(sock, proto.MSG_AUTH_RESPONSE, {
            "user": username,
            "pub": kx.public.hex(),
            "proof": proto.make_proof(password_key, b"client", transcript).hex(),
        })

        result = proto.recv_plain(sock, proto.MSG_AUTH_RESULT)
        if not result.get("ok"):
            raise AuthError(str(result.get("reason") or "Login abgelehnt."))
        host_proof = proto.hex_field(result, "proof", 32)
        expected = proto.make_proof(password_key, b"host", transcript)
        if not hmac.compare_digest(host_proof, expected):
            raise AuthError("Der Host konnte sich nicht ausweisen. Verbindung abgebrochen "
                            "(falscher Host oder Angriff im Netz).")

        host_to_client, client_to_host = kx.session_keys(host_pub, password_key, transcript)
        sock.settimeout(STREAM_TIMEOUT)
        return proto.SecureChannel(sock, client_to_host, host_to_client, MAX_FRAME)
    except Exception:
        sock.close()
        raise


# ---------------------------------------------------------------------------
# Tastenzuordnung Tk-Keysym -> pynput-Key-Name (wird auf dem Host aufgelöst)
# ---------------------------------------------------------------------------
SPECIAL_KEYS = {
    "Return": "enter", "KP_Enter": "enter", "BackSpace": "backspace", "Tab": "tab",
    "ISO_Left_Tab": "tab", "Escape": "esc", "Delete": "delete", "Insert": "insert",
    "Home": "home", "End": "end", "Prior": "page_up", "Next": "page_down",
    "Up": "up", "Down": "down", "Left": "left", "Right": "right", "space": "space",
    "Caps_Lock": "caps_lock", "Print": "print_screen", "Menu": "menu",
    "Shift_L": "shift", "Shift_R": "shift_r",
    "Control_L": "ctrl", "Control_R": "ctrl_r",
    "Alt_L": "alt", "Alt_R": "alt_r", "Option_L": "alt", "Option_R": "alt_r",
    "Meta_L": "cmd", "Meta_R": "cmd_r", "Command": "cmd",
    "Super_L": "cmd", "Super_R": "cmd_r", "Win_L": "cmd", "Win_R": "cmd_r",
}
SPECIAL_KEYS.update({f"F{i}": f"f{i}" for i in range(1, 21)})


def describe_key(event, cmd_as_ctrl):
    """Liefert {'key': name} für Sondertasten oder {'char': c} für Zeichen."""
    keysym = event.keysym
    if keysym in SPECIAL_KEYS:
        name = SPECIAL_KEYS[keysym]
        if cmd_as_ctrl:
            name = {"cmd": "ctrl", "cmd_r": "ctrl_r"}.get(name, name)
        return {"key": name}
    ch = event.char
    if ch and len(ch) == 1 and ch.isprintable():
        return {"char": ch}
    # Mit gedrückter Strg-Taste liefert Tk Steuerzeichen (z. B. '\x03' bei Strg+C),
    # dann den Buchstaben aus dem Keysym nehmen.
    if len(keysym) == 1 and keysym.isprintable():
        return {"char": keysym.lower()}
    return None


# ---------------------------------------------------------------------------
# Oberfläche
# ---------------------------------------------------------------------------
class ClientApp:
    def __init__(self, root, args, on_back=None):
        self.root = root
        self.args = args
        self.on_back = on_back
        self.channel = None
        self.running = False          # Empfang aktiv
        self.session_ui = False       # Anzeige-Fenster ist offen
        self.reconnecting = False
        self.conn_params = None       # (host, port, user, password) für Neuverbinden
        self.error = None
        self.overlay = None           # Banner "verbinde neu"
        self.notice_banner = None     # Banner mit Hinweis vom Host
        self.notice_incoming = None
        self.cursor_incoming = None   # (x, y, sichtbar) vom Host
        self.remote_pos = None
        self.ui_events = queue.Queue()  # Meldungen aus Threads für die Oberfläche
        self.sending_files = False
        self.drop_hint = None
        self.transfer_bar = None
        self.receiver = None
        self.remote_cursor = None     # Canvas-Element des fremden Mauszeigers
        self.last_send = 0.0
        self.cmd_as_ctrl = bool(args.cmd_as_ctrl)
        self.share_clipboard = True
        self.show_remote_cursor = True
        self.clip_last = None         # zuletzt abgeglichener Text der Zwischenablage
        self.clip_incoming = None     # vom Host empfangener Text (setzt der Hauptthread)

        self.frame_lock = threading.Lock()
        self.latest_frame = None      # zuletzt empfangenes Bild (vom Empfangs-Thread)
        self.current_frame = None     # aktuell angezeigtes Originalbild
        self.photo = None
        self.view = None              # (x, y, breite, höhe) des Bildes im Canvas
        self.last_canvas_size = None
        self.window_sized = False

        self.pressed_keys = {}        # Tk-Keycode -> gesendete Beschreibung
        self.pending_move = None
        self.move_scheduled = False
        self.scroll_acc = 0.0

        # Tk unter macOS < 8.7 vertauscht rechte (2) und mittlere (3) Maustaste
        tk_version = tuple(int(p) for p in root.tk.call("info", "patchlevel").split(".")[:2])
        if sys.platform == "darwin" and tk_version < (8, 7):
            self.button_map = {1: "left", 2: "right", 3: "middle"}
        else:
            self.button_map = {1: "left", 2: "middle", 3: "right"}

        root.title("HopGrid – Verbinden")
        root.protocol("WM_DELETE_WINDOW", self.close)
        if sys.platform == "darwin":
            # Cmd+Q soll nicht ohne Rückfrage die laufende Verbindung beenden
            root.createcommand("::tk::mac::Quit", self.close)
        self._build_login()

    # ----------------------------- Login -----------------------------
    def _build_login(self, message=""):
        self.root.configure(fg_color=ui.BG)
        self.login = ctk.CTkFrame(self.root, fg_color=ui.BG)
        self.login.pack(fill="both", expand=True)
        ui.header(self.login, "Mit anderem Rechner verbinden",
                  "Bildschirm eines freigegebenen Rechners im Netz sehen und steuern.",
                  on_back=self.go_back if self.on_back else None)

        body = ctk.CTkFrame(self.login, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=ui.S4, pady=(ui.S2, ui.S4))
        box = ui.card(body)
        box.pack(fill="x")
        form = ctk.CTkFrame(box, fg_color="transparent")
        form.pack(fill="x", padx=ui.S3 + 4, pady=ui.S3)

        saved = load_settings()
        # Ziel + Suchen
        target_row = ctk.CTkFrame(form, fg_color="transparent")
        target_row.pack(fill="x", pady=(0, ui.S2))
        target_row.grid_columnconfigure(0, weight=1)
        self.f_host = ui.Field(target_row, "Rechner (Alias oder IP-Adresse)",
                               self.args.host or saved.get("host", ""),
                               placeholder="z. B. Büro-PC oder 192.168.1.20")
        self.f_host.grid(row=0, column=0, sticky="new")
        self.search_btn = ui.secondary_button(target_row, "Suchen", self.start_search, width=88)
        self.search_btn.grid(row=0, column=1, sticky="nw", padx=(ui.S2, 0), pady=(21, 0))
        self.results = ctk.CTkFrame(form, fg_color="transparent")

        self.f_user = ui.Field(form, "Benutzername", self.args.user or saved.get("user", ""))
        self.f_user.pack(fill="x", pady=(0, ui.S2))
        self.f_pw = ui.Field(form, "Passwort", secret=True)
        self.f_pw.pack(fill="x", pady=(0, ui.S2))
        self._load_stored_password(self.f_host.get(), self.f_user.get())

        self.remember_var = tk.BooleanVar(value=saved.get("remember_password", True))
        if passwords.available():
            ui.checkbox(form, "Passwort speichern (im Schlüsselbund des Systems)",
                        self.remember_var).pack(anchor="w", pady=(ui.S1, ui.S1))
        self.clip_var = tk.BooleanVar(value=bool(saved.get("share_clipboard", True)))
        ui.checkbox(form, "Zwischenablage teilen (Kopieren & Einfügen)",
                    self.clip_var).pack(anchor="w", pady=(ui.S1, ui.S1))
        self.cursor_var = tk.BooleanVar(value=bool(saved.get("show_remote_cursor", True)))
        ui.checkbox(form, "Mauszeiger des anderen Rechners anzeigen",
                    self.cursor_var).pack(anchor="w", pady=(ui.S1, 0))

        advanced = ui.Collapsible(form, "Erweitert", on_toggle=self.fit_window
                                  ).pack(fill="x", pady=(ui.S2, 0))
        self.advanced = advanced
        self.f_port = ui.Field(advanced.body, "Port",
                               str(self.args.port or saved.get("port", proto.DEFAULT_PORT)),
                               hint="Wird bei der Suche per Alias automatisch ermittelt.",
                               width=120)
        self.f_port.pack(anchor="w", pady=(ui.S1, ui.S2))
        self.cmd_var = tk.BooleanVar(value=bool(saved.get("cmd_as_ctrl", self.cmd_as_ctrl)))
        ui.checkbox(advanced.body, "Cmd-/Windows-Taste als Strg senden (Mac steuert Windows)",
                    self.cmd_var).pack(anchor="w")

        self.status = ui.label(form, message, "small", ui.DANGER, anchor="w", justify="left",
                               wraplength=420)
        if message:
            self.status.pack(anchor="w", pady=(ui.S3, 0))
        self.connect_btn = ui.primary_button(form, "Verbinden", self.start_connect)
        self.connect_btn.pack(fill="x", pady=(ui.S3, 0))

        self.root.bind("<Return>", lambda _e: self.start_connect())
        self.root.after(10, self.fit_window)
        first = next((f for f in (self.f_host, self.f_user, self.f_pw) if not f.get()), self.f_pw)
        first.entry.focus_set()

    def _load_stored_password(self, host, user):
        """Gespeichertes Passwort im Hintergrund holen: macOS kann dafür eine
        Schlüsselbund-Abfrage zeigen, die sonst das Fenster blockiert."""
        if not host or not user or not passwords.available():
            return
        result = []

        def worker():
            result.append(passwords.load(host, user))

        def check():
            if not self.login.winfo_exists():
                return
            if not result:
                self.root.after(100, check)
                return
            stored = result[0]
            same_target = self.f_host.get() == host and self.f_user.get() == user
            if stored and same_target and not self.f_pw.get():
                self.f_pw.set(stored)
                self.f_pw.set_hint("✓  Gespeichertes Passwort wird verwendet.")
                self.f_pw.message.configure(text_color=ui.SUCCESS)

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(100, check)

    def fit_window(self):
        """Fensterhöhe an den Inhalt anpassen (kein Scrollen nötig)."""
        try:
            self.root.update_idletasks()
            self.root.tk.call("wm", "geometry", self.root._w, "")
        except tk.TclError:
            pass

    def set_status(self, text, kind="error"):
        color = {"error": ui.DANGER, "info": ui.MUTED}[kind]
        self.status.configure(text=text, text_color=color)
        if text:
            self.status.pack(anchor="w", pady=(ui.S3, 0), before=self.connect_btn)
        else:
            self.status.pack_forget()

    def go_back(self):
        if self.on_back and not self.session_ui:
            self.on_back()

    def destroy(self):
        self.session_ui = False
        self.running = False
        self._close_channel()
        for child in self.root.winfo_children():
            child.destroy()

    # ----------------------------- Suche -----------------------------
    def start_search(self):
        self.search_btn.configure(state="disabled", text="Suche …")
        self._search_result = None

        def worker():
            try:
                self._search_result = proto.discover()
            except OSError:
                self._search_result = []

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(100, self._check_search)

    def _check_search(self):
        if self._search_result is None:
            self.root.after(100, self._check_search)
            return
        self.search_btn.configure(state="normal", text="Suchen")
        for child in self.results.winfo_children():
            child.destroy()
        hosts = self._search_result
        self.results.pack(fill="x", pady=(0, ui.S2), after=self.f_host.frame.master)
        self.root.after(10, self.fit_window)
        if not hosts:
            ui.label(self.results, "Kein freigegebener Rechner gefunden. Läuft auf dem anderen "
                                   "Rechner die Freigabe?", "small", ui.MUTED, anchor="w",
                     justify="left", wraplength=420).pack(anchor="w")
            return
        ui.label(self.results, "Gefunden – zum Übernehmen anklicken:", "small", ui.MUTED
                 ).pack(anchor="w", pady=(0, ui.S1))
        for found in hosts[:6]:
            ctk.CTkButton(self.results, text=f"{found['alias']}     {found['ip']}:{found['port']}",
                          command=lambda h=found: self._pick_host(h), anchor="w", height=34,
                          corner_radius=8, fg_color=ui.SECONDARY, hover_color=ui.SECONDARY_HOVER,
                          text_color=ui.TEXT, font=ui.font("body")).pack(fill="x", pady=2)

    def _pick_host(self, found):
        self.f_host.set(found["alias"])
        self.f_port.set(str(found["port"]))
        self.results.pack_forget()
        self.f_pw.set("")
        self._load_stored_password(found["alias"], self.f_user.get())
        (self.f_user if not self.f_user.get() else self.f_pw).entry.focus_set()

    # ----------------------------- Verbinden -----------------------------
    def start_connect(self):
        if self.session_ui or str(self.connect_btn.cget("state")) == "disabled":
            return
        host = self.f_host.get().strip()
        user = self.f_user.get().strip()
        password = self.f_pw.get()
        try:
            port = int(self.f_port.get().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            if not self.advanced.is_open:
                self.advanced.toggle()
            self.f_port.error("Bitte eine Zahl von 1 bis 65535 eingeben.")
            return
        if not host:
            self.f_host.error("Bitte Alias oder IP-Adresse eingeben – oder auf „Suchen“ klicken.")
            return
        if not user:
            self.f_user.error("Bitte den Benutzernamen der Freigabe eingeben.")
            return
        if not password:
            self.f_pw.error("Bitte das Passwort der Freigabe eingeben.")
            return
        self.set_status("")

        remember = self.remember_var.get()
        self.cmd_as_ctrl = self.cmd_var.get()
        self.share_clipboard = self.clip_var.get()
        self.show_remote_cursor = self.cursor_var.get()
        # Einstellungen sofort merken, nicht erst nach erfolgreicher Verbindung
        save_settings({"host": host, "port": port, "user": user,
                       "remember_password": remember, "cmd_as_ctrl": self.cmd_as_ctrl,
                       "share_clipboard": self.share_clipboard,
                       "show_remote_cursor": self.show_remote_cursor})

        self.connect_btn.configure(state="disabled", text="Verbinde …")
        self._connect_result = None
        threading.Thread(target=self._connect_worker,
                         args=(host, port, user, password, remember), daemon=True).start()
        self.root.after(50, self._check_connect)

    def _connect_worker(self, host, port, user, password, remember):
        try:
            ip, real_port = resolve_target(host, port)
            channel = connect_and_authenticate(ip, real_port, user, password)
            if remember:
                passwords.store(host, user, password)
            else:
                passwords.forget(host, user)
            self._connect_result = ("ok", channel, (host, port, user, password))
        except (AuthError, HostNotFound) as exc:
            self._connect_result = ("error", str(exc), None)
        except (OSError, ConnectionError, ValueError) as exc:
            self._connect_result = ("error", friendly_error(exc), None)

    def _check_connect(self):
        if self._connect_result is None:
            self.root.after(50, self._check_connect)
            return
        kind, value, params = self._connect_result
        if kind == "error":
            self.connect_btn.configure(state="normal", text="Verbinden")
            if "Passwort" in value:
                self.f_pw.error(value)
            elif "nicht gefunden" in value:
                self.f_host.error(value)
            else:
                self.set_status(value)
            return
        self.conn_params = params
        self._start_session(params[0], value)

    # ----------------------------- Sitzung -----------------------------
    def _start_session(self, host, channel):
        self.root.unbind("<Return>")
        self.login.destroy()
        self.root.title(f"HopGrid – {host}")
        self.root.geometry("1024x640")

        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0, takefocus=True)
        self.canvas.pack(fill="both", expand=True)
        self.image_item = self.canvas.create_image(0, 0, anchor="nw")

        c = self.canvas
        c.bind("<Motion>", self.on_motion)
        c.bind("<ButtonPress>", self.on_button_down)
        c.bind("<ButtonRelease>", self.on_button_up)
        c.bind("<MouseWheel>", self.on_wheel)
        c.bind("<KeyPress>", self.on_key_down)
        c.bind("<KeyRelease>", self.on_key_up)
        c.bind("<FocusOut>", self.on_focus_out)
        c.bind("<Enter>", lambda _e: c.focus_set())
        c.focus_set()

        self.transfer_bar = ui.TransferBar(self.root)
        self._setup_file_drop()

        self.session_ui = True
        self._attach(channel)
        self.root.after(RENDER_INTERVAL_MS, self._render_tick)
        self.root.after(PING_INTERVAL_MS, self._ping_tick)
        self.root.after(500, self._clipboard_tick)

    def _attach(self, channel):
        """Neuen (oder wiederhergestellten) Kanal in Betrieb nehmen."""
        self.channel = channel
        self.error = None
        self.running = True
        self.last_send = time.monotonic()
        self.clip_last = None  # aktuelle Zwischenablage beim (Neu-)Verbinden an den Host geben
        self.sending_files = False
        self.receiver = filetransfer.Receiver(
            channel, filetransfer.downloads_dir(),
            lambda kind, data: self.ui_events.put(("recv", kind, data)))
        threading.Thread(target=self._receive_loop, args=(channel,), daemon=True).start()

    def _receive_loop(self, channel):
        """Eigener Thread: Bilder empfangen und dekodieren."""
        try:
            while self.running and channel is self.channel:
                msg_type, payload = channel.recv()
                if msg_type == proto.MSG_CLIPBOARD:
                    self.clip_incoming = payload.decode("utf-8", errors="replace")
                    continue
                if msg_type == proto.MSG_CURSOR:
                    try:
                        data = json.loads(payload)
                        self.cursor_incoming = (float(data["x"]), float(data["y"]),
                                                bool(data["v"]))
                    except (ValueError, KeyError, TypeError):
                        pass
                    continue
                if msg_type == proto.MSG_NOTICE:
                    self.notice_incoming = payload.decode("utf-8", errors="replace")
                    continue
                if self.receiver is not None and self.receiver.handle(msg_type, payload):
                    continue
                if msg_type in (proto.MSG_FILES_OFFER, proto.MSG_XFER_ACK):
                    try:
                        data = json.loads(payload)
                    except ValueError:
                        continue
                    self.ui_events.put(("offer" if msg_type == proto.MSG_FILES_OFFER else "ack",
                                        data))
                    continue
                if msg_type != proto.MSG_FRAME:
                    continue  # z. B. Lebenszeichen (PING)
                try:
                    image = Image.open(io.BytesIO(payload))
                    image.load()
                except Exception:
                    continue  # einzelnes kaputtes Bild überspringen
                with self.frame_lock:
                    self.latest_frame = image
        except Exception as exc:
            if self.running and channel is self.channel:
                self.error = exc

    def _render_tick(self):
        """Hauptthread: neuestes Bild skaliert in das Fenster zeichnen."""
        if not self.session_ui:
            return
        if self.error is not None and not self.reconnecting:
            self._begin_reconnect()

        with self.frame_lock:
            new_frame, self.latest_frame = self.latest_frame, None
        if new_frame is not None:
            self.current_frame = new_frame
            if not self.window_sized:
                self._fit_window(new_frame.size)

        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if (self.current_frame is not None and cw > 1 and ch > 1
                and (new_frame is not None or (cw, ch) != self.last_canvas_size)):
            iw, ih = self.current_frame.size
            scale = min(cw / iw, ch / ih)
            dw, dh = max(1, int(iw * scale)), max(1, int(ih * scale))
            shown = self.current_frame
            if (dw, dh) != (iw, ih):
                shown = shown.resize((dw, dh), Image.Resampling.BICUBIC, reducing_gap=2.0)
            self.photo = ImageTk.PhotoImage(shown)
            ox, oy = (cw - dw) // 2, (ch - dh) // 2
            self.canvas.itemconfigure(self.image_item, image=self.photo)
            self.canvas.coords(self.image_item, ox, oy)
            self.view = (ox, oy, dw, dh)
            self.last_canvas_size = (cw, ch)
            self._draw_remote_cursor()

        cursor, self.cursor_incoming = self.cursor_incoming, None
        if cursor is not None:
            self.remote_pos = cursor
            self._draw_remote_cursor()

        while True:
            try:
                self._handle_ui_event(self.ui_events.get_nowait())
            except queue.Empty:
                break

        notice, self.notice_incoming = self.notice_incoming, None
        if notice is not None:
            if notice:
                if self.notice_banner is None:
                    self.notice_banner = ui.Banner(self.root)
                self.notice_banner.show(notice, "warn")
            elif self.notice_banner is not None:
                self.notice_banner.hide()
                self.notice_banner = None

        self.root.after(RENDER_INTERVAL_MS, self._render_tick)

    # Pfeil-Umriss (Spitze bei 0,0), etwa 18 px hoch
    CURSOR_SHAPE = [(0, 0), (0, 17), (4.5, 13), (7.5, 19.5), (10, 18.5), (7, 12), (12.5, 12)]

    def _draw_remote_cursor(self):
        """Mauszeiger des Hosts an seiner Position über das Bild zeichnen."""
        pos = getattr(self, "remote_pos", None)
        if pos is None or not self.view or not self.show_remote_cursor:
            return
        nx, ny, visible = pos
        ox, oy, dw, dh = self.view
        x, y = ox + nx * dw, oy + ny * dh
        points = [coord for px, py in self.CURSOR_SHAPE for coord in (x + px, y + py)]
        if self.remote_cursor is None:
            self.remote_cursor = self.canvas.create_polygon(
                points, fill="white", outline="black", width=1.2)
        else:
            self.canvas.coords(self.remote_cursor, *points)
        self.canvas.itemconfigure(self.remote_cursor, state="normal" if visible else "hidden")
        self.canvas.tag_raise(self.remote_cursor)

    def _ping_tick(self):
        """Lebenszeichen an den Host, damit er tote Verbindungen erkennt."""
        if not self.session_ui:
            return
        if self.running and time.monotonic() - self.last_send >= PING_INTERVAL_MS / 1000:
            self._send(proto.MSG_PING, b"")
        self.root.after(1000, self._ping_tick)

    def _clipboard_tick(self):
        """Zwischenablage abgleichen: lokale Änderungen an den Host, Host-Text lokal setzen."""
        if not self.session_ui:
            return
        if self.share_clipboard:
            incoming, self.clip_incoming = self.clip_incoming, None
            if incoming is not None:
                try:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(incoming)
                    self.clip_last = incoming
                except tk.TclError:
                    pass
            elif self.running and not self.reconnecting:
                try:
                    text = self.root.clipboard_get()
                except tk.TclError:
                    text = None  # leer oder kein Text (z. B. Bild)
                if text and text != self.clip_last:
                    self.clip_last = text
                    data = text.encode("utf-8")
                    if len(data) <= proto.MAX_CLIPBOARD:
                        self._send(proto.MSG_CLIPBOARD, data)
        self.root.after(500, self._clipboard_tick)

    def _fit_window(self, size):
        """Beim ersten Bild Fenstergröße an das Seitenverhältnis des Hosts anpassen."""
        iw, ih = size
        max_w = int(self.root.winfo_screenwidth() * 0.9)
        max_h = int(self.root.winfo_screenheight() * 0.85)
        scale = min(max_w / iw, max_h / ih, 1.0)
        self.root.geometry(f"{int(iw * scale)}x{int(ih * scale)}")
        self.window_sized = True

    # ----------------------------- Neuverbinden -----------------------------
    def _show_overlay(self, text):
        if self.overlay is None:
            self.overlay = ui.Banner(self.root)
        self.overlay.show(text, "info", rely=0.5, anchor="center", y=0)

    def _hide_overlay(self):
        if self.overlay is not None:
            self.overlay.hide()
            self.overlay = None

    def _begin_reconnect(self):
        self.reconnecting = True
        self.running = False
        self._close_channel()
        self.pressed_keys.clear()
        self._reconnect_started = time.monotonic()
        self._show_overlay("Verbindung unterbrochen – verbinde neu …")
        self._reconnect_result = None
        threading.Thread(target=self._reconnect_worker, daemon=True).start()
        self.root.after(250, self._check_reconnect)

    def _reconnect_worker(self):
        host, port, user, password = self.conn_params
        deadline = time.monotonic() + RECONNECT_SECONDS
        last_error = "Zeit abgelaufen"
        while self.session_ui and time.monotonic() < deadline:
            try:
                ip, real_port = resolve_target(host, port)
                channel = connect_and_authenticate(ip, real_port, user, password)
                self._reconnect_result = ("ok", channel)
                return
            except AuthError as exc:  # Passwort geändert o. Ä. – Wiederholen sinnlos
                self._reconnect_result = ("error", str(exc))
                return
            except (OSError, ConnectionError, ValueError) as exc:
                last_error = str(exc)
            time.sleep(RECONNECT_PAUSE)
        self._reconnect_result = ("error", last_error)

    def _check_reconnect(self):
        if not self.session_ui:
            return
        if self._reconnect_result is None:
            waited = int(time.monotonic() - self._reconnect_started)
            self._show_overlay(f"Verbindung unterbrochen – verbinde neu … ({waited} s)")
            self.root.after(250, self._check_reconnect)
            return
        kind, value = self._reconnect_result
        self.reconnecting = False
        if kind == "ok":
            self._hide_overlay()
            self._attach(value)
            return
        self._end_session(f"Verbindung verloren: {value}")

    def _close_channel(self):
        if self.channel is not None:
            try:
                self.channel.sock.close()
            except OSError:
                pass

    def _end_session(self, message=""):
        """Zurück zum Login-Fenster (statt die App zu beenden)."""
        self.session_ui = False
        self.running = False
        self._close_channel()
        self.channel = None
        self._hide_overlay()
        if self.notice_banner is not None:
            self.notice_banner.hide()
            self.notice_banner = None
        self.remote_cursor = None
        self.remote_pos = None
        if self.transfer_bar is not None:
            self.transfer_bar.hide()
            self.transfer_bar = None
        self.receiver = None
        self.canvas.destroy()
        self.current_frame = self.latest_frame = self.photo = self.view = None
        self.last_canvas_size = None
        self.window_sized = False
        self.pressed_keys.clear()
        self.root.title("HopGrid – Verbinden")
        self._build_login(message)

    # ----------------------------- Dateien -----------------------------
    def _setup_file_drop(self):
        """Dateien/Ordner auf das Fenster ziehen = auf den anderen Rechner kopieren."""
        if not getattr(self.root, "dnd_available", False):
            return
        from tkinterdnd2 import DND_FILES
        self.canvas.drop_target_register(DND_FILES)
        self.canvas.dnd_bind("<<DropEnter>>", self._on_drop_enter)
        self.canvas.dnd_bind("<<DropPosition>>", lambda e: e.action)
        self.canvas.dnd_bind("<<DropLeave>>", self._on_drop_leave)
        self.canvas.dnd_bind("<<Drop>>", self._on_drop)

    def _on_drop_enter(self, event):
        if self.drop_hint is None:
            self.drop_hint = ui.Banner(self.root)
        self.drop_hint.show("Loslassen, um auf den anderen Rechner zu kopieren",
                            "info", rely=0.5, anchor="center", y=0)
        return event.action

    def _on_drop_leave(self, event):
        if self.drop_hint is not None:
            self.drop_hint.hide()
            self.drop_hint = None
        return event.action

    def _on_drop(self, event):
        self._on_drop_leave(event)
        paths = [p for p in self.root.tk.splitlist(event.data) if os.path.exists(p)]
        if not paths:
            return event.action
        if not self.running or self.reconnecting:
            self.transfer_bar.show("Gerade keine Verbindung – bitte gleich noch einmal.", "error")
            return event.action
        if self.sending_files:
            self.transfer_bar.show("Es läuft schon eine Übertragung – bitte warten.", "error")
            return event.action
        self.sending_files = True
        names = [os.path.basename(p.rstrip(os.sep)) for p in paths]
        self.transfer_bar.show(f"Sende {filetransfer.describe(names, len(paths))} …", "progress",
                               fraction=0.0)
        threading.Thread(target=self._send_files, args=(paths, names), daemon=True).start()
        return event.action

    def _send_files(self, paths, names):
        def progress(done, total):
            self.ui_events.put(("send_progress", names, done, total))
        try:
            filetransfer.send_paths(self.channel, paths, progress)
        except OSError as exc:
            self.ui_events.put(("send_error", str(exc)))

    def request_offered_files(self):
        self.transfer_bar.show("Hole Dateien vom anderen Rechner …", "progress", fraction=0.0)
        self._send(proto.MSG_FILES_REQUEST, b"")

    def _handle_ui_event(self, item):
        kind = item[0]
        bar = self.transfer_bar
        if kind == "send_progress":
            _, names, done, total = item
            pct = done / total if total else 1.0
            bar.show(f"Sende {filetransfer.describe(names, len(names))} … {int(pct * 100)} %",
                     "progress", fraction=pct)
        elif kind == "send_error":
            self.sending_files = False
            bar.show(f"Senden abgebrochen: {item[1]}", "error")
        elif kind == "ack":
            self.sending_files = False
            data = item[1]
            if data.get("ok"):
                bar.show("Auf dem anderen Rechner in Downloads › HopGrid abgelegt.", "success",
                         hide_after=6000)
            else:
                bar.show(f"Der andere Rechner konnte die Dateien nicht speichern: "
                         f"{data.get('error')}", "error")
        elif kind == "offer":
            data = item[1]
            if int(data.get("count", 0)) > 0:
                text = (f"Auf dem anderen Rechner kopiert: "
                        f"{filetransfer.describe(data.get('names', []), int(data['count']))} "
                        f"({filetransfer.human_size(int(data.get('bytes', 0)))})")
                bar.show(text, "offer", action=("Hierher holen", self.request_offered_files))
            elif bar.kind == "offer":
                bar.hide()
        elif kind == "recv":
            _, event, data = item
            if event == "start":
                bar.show(f"Empfange {filetransfer.describe(data['names'], data['count'])} …",
                         "progress", fraction=0.0)
            elif event == "progress":
                pct = data["done"] / data["total"] if data["total"] else 1.0
                bar.show(f"Empfange … {int(pct * 100)} %", "progress", fraction=pct)
            elif event == "done":
                if data["ok"]:
                    bar.show(f"{filetransfer.describe(data['names'], data['count'])} liegt in "
                             "Downloads › HopGrid", "received", files=data["saved"])
                else:
                    bar.show(f"Empfang fehlgeschlagen: {data['error']}", "error")

    # ----------------------------- Eingaben -----------------------------
    def _send(self, msg_type, payload):
        if not self.running or self.reconnecting:
            return
        try:
            self.channel.send(msg_type, payload)
            self.last_send = time.monotonic()
        except OSError as exc:
            self.error = exc

    def send_input(self, event):
        self._send(proto.MSG_INPUT, json.dumps(event).encode("utf-8"))

    def to_normalized(self, x, y, clamp=False):
        """Fensterkoordinaten -> 0..1 relativ zum Host-Bild. None, wenn außerhalb."""
        if not self.view:
            return None
        ox, oy, dw, dh = self.view
        nx, ny = (x - ox) / dw, (y - oy) / dh
        if clamp:
            nx, ny = min(max(nx, 0.0), 1.0), min(max(ny, 0.0), 1.0)
        elif not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
            return None
        return round(nx, 5), round(ny, 5)

    def on_motion(self, event):
        pos = self.to_normalized(event.x, event.y)
        if pos is None:
            return
        self.pending_move = pos
        if not self.move_scheduled:
            self.move_scheduled = True
            self.root.after(MOVE_INTERVAL_MS, self._flush_move)

    def _flush_move(self):
        self.move_scheduled = False
        if self.pending_move is not None:
            x, y = self.pending_move
            self.pending_move = None
            self.send_input({"t": "move", "x": x, "y": y})

    def _send_button(self, event, action):
        name = self.button_map.get(event.num)
        if name is None:
            return
        pos = self.to_normalized(event.x, event.y, clamp=(action == "up"))
        if pos is None:
            return
        self._flush_move()
        self.send_input({"t": "btn", "b": name, "a": action, "x": pos[0], "y": pos[1]})

    def on_button_down(self, event):
        self.canvas.focus_set()
        self._send_button(event, "down")

    def on_button_up(self, event):
        self._send_button(event, "up")

    def on_wheel(self, event):
        # Windows: Vielfache von 120 pro Raste; macOS (Tk 8.6): kleine Werte
        delta = event.delta
        self.scroll_acc += delta / 120.0 if abs(delta) >= 120 else float(delta)
        steps = int(self.scroll_acc)
        if steps:
            self.scroll_acc -= steps
            self.send_input({"t": "scroll", "dy": steps})

    def on_key_down(self, event):
        desc = describe_key(event, self.cmd_as_ctrl)
        if desc is not None:
            self.pressed_keys[event.keycode] = desc
            self.send_input({"t": "key", "a": "down", **desc})
        return "break"  # verhindert lokale Tk-Aktionen (z. B. Fokuswechsel mit Tab)

    def on_key_up(self, event):
        desc = self.pressed_keys.pop(event.keycode, None) or describe_key(event, self.cmd_as_ctrl)
        if desc is not None:
            self.send_input({"t": "key", "a": "up", **desc})
        return "break"

    def on_focus_out(self, _event):
        """Fenster verliert den Fokus (z. B. Alt+Tab): gedrückte Tasten freigeben."""
        for desc in self.pressed_keys.values():
            self.send_input({"t": "key", "a": "up", **desc})
        self.pressed_keys.clear()

    # ----------------------------- Ende -----------------------------
    def close(self):
        if self.session_ui and not messagebox.askokcancel(
                "HopGrid", "Verbindung trennen und HopGrid beenden?", parent=self.root):
            return
        self.session_ui = False
        self.running = False
        self._close_channel()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="HopGrid Client – Host im LAN fernsteuern")
    parser.add_argument("--host", help="Alias oder IP-Adresse des Hosts (vorausgefüllt)")
    parser.add_argument("--port", type=int, help=f"Port (Standard {proto.DEFAULT_PORT})")
    parser.add_argument("--user", help="Benutzername (vorausgefüllt)")
    parser.add_argument("--cmd-as-ctrl", action="store_true",
                        help="Cmd/Windows-Taste als Strg senden (Mac steuert Windows)")
    # parse_known_args: macOS kann beim App-Start eigene Argumente (-psn_…) anhängen
    args, _unknown = parser.parse_known_args()

    enable_dpi_awareness()
    ui.setup()
    root = ui.create_root()
    root.minsize(500, 300)
    ui.set_window_icon(root)
    ClientApp(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()

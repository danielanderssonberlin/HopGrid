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
import hmac
import io
import ipaddress
import json
import socket
import sys
import threading
import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageTk

import autostart
import protocol as proto

MAX_FRAME = 50 * 1024 * 1024
CONNECT_TIMEOUT = 5
AUTH_TIMEOUT = 15
STREAM_TIMEOUT = 10        # Host sendet spätestens alle 2 s ein Bild
MOVE_INTERVAL_MS = 12      # Mausbewegungen bündeln (ca. 80 pro Sekunde)
RENDER_INTERVAL_MS = 15
CLIENT_CONFIG = "client.json"   # gemerkte Eingaben (ohne Passwort)


class AuthError(Exception):
    pass


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
        raise AuthError(f"„{target}“ nicht gefunden. Läuft der Host mit diesem Alias "
                        "im selben Netz und ist die Freigabe gestartet?") from None


def connect_and_authenticate(host, port, username, password):
    """Baut die Verbindung auf, meldet sich an und liefert einen verschlüsselten Kanal.

    Das Passwort selbst wird nie gesendet. Der Host muss seinerseits beweisen, dass er
    das Passwort kennt, sonst bricht der Client ab (Schutz vor gefälschtem Host)."""
    sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(AUTH_TIMEOUT)

        challenge = proto.recv_plain(sock, proto.MSG_CHALLENGE)
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
    def __init__(self, root, args):
        self.root = root
        self.args = args
        self.channel = None
        self.running = False
        self.error = None

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
        self._build_login()

    # ----------------------------- Login -----------------------------
    def _build_login(self):
        self.login = tk.Frame(self.root, padx=20, pady=20)
        self.login.pack(fill="both", expand=True)

        saved = load_settings()
        fields = [("Host (Alias oder IP)", self.args.host or saved.get("host", "")),
                  ("Port", str(self.args.port or saved.get("port", proto.DEFAULT_PORT))),
                  ("Benutzername", self.args.user or saved.get("user", "")),
                  ("Passwort", "")]
        self.entries = []
        for row, (label, value) in enumerate(fields):
            tk.Label(self.login, text=label).grid(row=row, column=0, sticky="w", pady=4)
            entry = tk.Entry(self.login, width=28, show="•" if label == "Passwort" else "")
            entry.insert(0, value)
            entry.grid(row=row, column=1, pady=4)
            self.entries.append(entry)

        self.search_btn = tk.Button(self.login, text="Suchen …", command=self.start_search)
        self.search_btn.grid(row=0, column=2, padx=(8, 0))

        self.status = tk.Label(self.login, text="", fg="#b00020")
        self.status.grid(row=4, column=0, columnspan=3, pady=(8, 0))
        self.connect_btn = tk.Button(self.login, text="Verbinden", command=self.start_connect)
        self.connect_btn.grid(row=5, column=0, columnspan=3, pady=(10, 0))

        self.root.bind("<Return>", lambda _e: self.start_connect())
        first_empty = next((e for e in self.entries if not e.get()), self.entries[-1])
        first_empty.focus_set()

    # ----------------------------- Suche -----------------------------
    def start_search(self):
        self.search_btn.config(state="disabled")
        self.status.config(text="Suche Hosts im Netz …", fg="#444")
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
        self.search_btn.config(state="normal")
        hosts = self._search_result
        if not hosts:
            self.status.config(text="Kein Host gefunden. Ist die Freigabe gestartet?",
                               fg="#b00020")
            return
        self.status.config(text=f"{len(hosts)} Host(s) gefunden – bitte auswählen.", fg="#444")
        menu = tk.Menu(self.root, tearoff=0)
        for found in hosts:
            menu.add_command(label=f"{found['alias']}   ({found['ip']}:{found['port']})",
                             command=lambda h=found: self._pick_host(h))
        x = self.search_btn.winfo_rootx()
        y = self.search_btn.winfo_rooty() + self.search_btn.winfo_height()
        menu.tk_popup(x, y)

    def _pick_host(self, found):
        self.entries[0].delete(0, "end")
        self.entries[0].insert(0, found["alias"])
        self.entries[1].delete(0, "end")
        self.entries[1].insert(0, str(found["port"]))
        self.status.config(text="")
        target = self.entries[2] if not self.entries[2].get() else self.entries[3]
        target.focus_set()

    # ----------------------------- Verbinden -----------------------------
    def start_connect(self):
        if self.running or str(self.connect_btn["state"]) == "disabled":
            return
        host = self.entries[0].get().strip()
        user = self.entries[2].get().strip()
        password = self.entries[3].get()
        try:
            port = int(self.entries[1].get().strip())
        except ValueError:
            self.status.config(text="Port muss eine Zahl sein.")
            return
        if not host or not user or not password:
            self.status.config(text="Bitte alle Felder ausfüllen.")
            return

        self.connect_btn.config(state="disabled")
        self.status.config(text="Suche Host und verbinde …", fg="#444")
        self._connect_result = None
        threading.Thread(target=self._connect_worker,
                         args=(host, port, user, password), daemon=True).start()
        self.root.after(50, lambda: self._check_connect(host))

    def _connect_worker(self, host, port, user, password):
        try:
            ip, real_port = resolve_target(host, port)
            channel = connect_and_authenticate(ip, real_port, user, password)
            save_settings({"host": host, "port": port, "user": user})
            self._connect_result = ("ok", channel)
        except AuthError as exc:
            self._connect_result = ("error", str(exc))
        except (OSError, ConnectionError, ValueError) as exc:
            self._connect_result = ("error", f"Verbindung fehlgeschlagen: {exc}")

    def _check_connect(self, host):
        if self._connect_result is None:
            self.root.after(50, lambda: self._check_connect(host))
            return
        kind, value = self._connect_result
        if kind == "error":
            self.status.config(text=value, fg="#b00020")
            self.connect_btn.config(state="normal")
            self.entries[3].delete(0, "end")
            self.entries[3].focus_set()
            return
        self.channel = value
        self._start_session(host)

    # ----------------------------- Sitzung -----------------------------
    def _start_session(self, host):
        self.root.unbind("<Return>")
        self.login.destroy()
        self.root.title(f"HopGrid – {host}")
        self.root.geometry("1024x640")

        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0,
                                cursor="crosshair", takefocus=True)
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

        self.running = True
        threading.Thread(target=self._receive_loop, daemon=True).start()
        self.root.after(RENDER_INTERVAL_MS, self._render_tick)

    def _receive_loop(self):
        """Eigener Thread: Bilder empfangen und dekodieren."""
        try:
            while self.running:
                msg_type, payload = self.channel.recv()
                if msg_type != proto.MSG_FRAME:
                    continue
                image = Image.open(io.BytesIO(payload))
                image.load()
                with self.frame_lock:
                    self.latest_frame = image
        except Exception as exc:
            if self.running:
                self.error = exc

    def _render_tick(self):
        """Hauptthread: neuestes Bild skaliert in das Fenster zeichnen."""
        if not self.running:
            return
        if self.error is not None:
            self._connection_lost()
            return

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
                shown = shown.resize((dw, dh), Image.Resampling.BILINEAR)
            self.photo = ImageTk.PhotoImage(shown)
            ox, oy = (cw - dw) // 2, (ch - dh) // 2
            self.canvas.itemconfigure(self.image_item, image=self.photo)
            self.canvas.coords(self.image_item, ox, oy)
            self.view = (ox, oy, dw, dh)
            self.last_canvas_size = (cw, ch)

        self.root.after(RENDER_INTERVAL_MS, self._render_tick)

    def _fit_window(self, size):
        """Beim ersten Bild Fenstergröße an das Seitenverhältnis des Hosts anpassen."""
        iw, ih = size
        max_w = int(self.root.winfo_screenwidth() * 0.9)
        max_h = int(self.root.winfo_screenheight() * 0.85)
        scale = min(max_w / iw, max_h / ih, 1.0)
        self.root.geometry(f"{int(iw * scale)}x{int(ih * scale)}")
        self.window_sized = True

    # ----------------------------- Eingaben -----------------------------
    def send_input(self, event):
        if not self.running:
            return
        try:
            self.channel.send(proto.MSG_INPUT, json.dumps(event).encode("utf-8"))
        except OSError as exc:
            self.error = exc

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
        desc = describe_key(event, self.args.cmd_as_ctrl)
        if desc is not None:
            self.pressed_keys[event.keycode] = desc
            self.send_input({"t": "key", "a": "down", **desc})
        return "break"  # verhindert lokale Tk-Aktionen (z. B. Fokuswechsel mit Tab)

    def on_key_up(self, event):
        desc = self.pressed_keys.pop(event.keycode, None) or describe_key(
            event, self.args.cmd_as_ctrl)
        if desc is not None:
            self.send_input({"t": "key", "a": "up", **desc})
        return "break"

    def on_focus_out(self, _event):
        """Fenster verliert den Fokus (z. B. Alt+Tab): gedrückte Tasten freigeben."""
        for desc in self.pressed_keys.values():
            self.send_input({"t": "key", "a": "up", **desc})
        self.pressed_keys.clear()

    # ----------------------------- Ende -----------------------------
    def _connection_lost(self):
        exc = self.error
        self.running = False
        messagebox.showerror("HopGrid", f"Verbindung zum Host verloren:\n{exc}")
        self.close()

    def close(self):
        self.running = False
        if self.channel is not None:
            try:
                self.channel.sock.close()
            except OSError:
                pass
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

    root = tk.Tk()
    ClientApp(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
HopGrid – eine App für beide Rollen.

Beim Start wählt man:
    - "Diesen Rechner freigeben"        -> Host (Bildschirm teilen, steuern lassen)
    - "Mit anderem Rechner verbinden"   -> Client (fremden Bildschirm sehen und steuern)

Start:
    python hopgrid.py                 # Auswahlfenster
    python hopgrid.py --mode host     # direkt freigeben
    python hopgrid.py --mode client --host Büro-PC
    python hopgrid.py --autostart     # wie --mode host, startet sofort mit gespeicherter Freigabe
"""

import argparse
import tkinter as tk

import autostart
import protocol as proto
from dpi import enable_dpi_awareness


def parse_args():
    parser = argparse.ArgumentParser(description="HopGrid – Fernzugriff im lokalen Netz")
    parser.add_argument("--mode", choices=["host", "client"], help="Auswahl überspringen")
    parser.add_argument(autostart.AUTOSTART_ARG, dest="autostart", action="store_true",
                        help="Freigabe sofort mit gespeicherten Daten starten")
    # Gemeinsam
    parser.add_argument("--port", type=int, help=f"Port (Standard {proto.DEFAULT_PORT})")
    parser.add_argument("--user", help="Benutzername (vorausgefüllt)")
    # Client
    parser.add_argument("--host", help="Client: Alias oder IP des Hosts")
    parser.add_argument("--cmd-as-ctrl", action="store_true",
                        help="Client: Cmd/Windows-Taste als Strg senden (Mac steuert Windows)")
    # Host
    parser.add_argument("--alias", help="Host: Name im LAN (Standard: Rechnername)")
    parser.add_argument("--bind", default="0.0.0.0", help="Host: Adresse zum Lauschen")
    parser.add_argument("--fps", type=int, default=15, help="Host: Bilder pro Sekunde (1-60)")
    parser.add_argument("--quality", type=int, default=85, help="Host: JPEG-Qualität (10-95)")
    parser.add_argument("--max-width", type=int, default=2560, help="Host: maximale Bildbreite")
    parser.add_argument("--monitor", type=int, default=1, help="Host: Monitor-Nummer")
    parser.add_argument("--allow-public", action="store_true",
                        help="Host: auch nicht-private IPs erlauben (nicht empfohlen)")
    # parse_known_args: macOS kann beim App-Start eigene Argumente (-psn_…) anhängen
    args, _unknown = parser.parse_known_args()
    if args.autostart:
        args.mode = "host"
    return args


def open_host(root, args):
    import host  # lädt OpenCV/mss/pynput erst, wenn sie gebraucht werden
    host.HostWindow(root, args, autostart_launch=args.autostart)


def open_client(root, args):
    import client
    client.ClientApp(root, args)


class StartScreen:
    def __init__(self, root, args):
        self.root = root
        self.args = args
        root.title("HopGrid")
        root.resizable(False, False)

        self.frame = tk.Frame(root, padx=28, pady=24)
        self.frame.pack()
        tk.Label(self.frame, text="HopGrid", font=("TkDefaultFont", 20, "bold")).pack()
        tk.Label(self.frame, text="Fernzugriff im lokalen Netz – verschlüsselt, ohne Cloud",
                 fg="#555").pack(pady=(2, 18))

        self._choice("Diesen Rechner freigeben",
                     "Andere dürfen diesen Bildschirm sehen und steuern.", "host")
        self._choice("Mit anderem Rechner verbinden",
                     "Einen freigegebenen Rechner sehen und steuern.", "client")

    def _choice(self, title, text, mode):
        button = tk.Button(self.frame, text=title, width=32, pady=8,
                           font=("TkDefaultFont", 13, "bold"),
                           command=lambda: self.select(mode))
        button.pack(pady=(6, 0))
        tk.Label(self.frame, text=text, fg="#555").pack(pady=(2, 8))

    def select(self, mode):
        self.frame.destroy()
        self.root.resizable(True, True)
        if mode == "host":
            open_host(self.root, self.args)
        else:
            open_client(self.root, self.args)


def main():
    enable_dpi_awareness()  # vor dem ersten Fenster, sonst unscharf und Maus-Versatz
    args = parse_args()
    root = tk.Tk()
    if args.mode == "host":
        open_host(root, args)
    elif args.mode == "client":
        open_client(root, args)
    else:
        StartScreen(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()

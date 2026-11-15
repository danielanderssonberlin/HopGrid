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
import sys

import customtkinter as ctk

import autostart
import protocol as proto
import ui
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


class App:
    """Hält das Hauptfenster und wechselt zwischen Start, Freigabe und Verbinden."""

    def __init__(self, root, args):
        self.root = root
        self.args = args
        self.screen = None
        root.title("HopGrid")
        root.configure(fg_color=ui.BG)
        ui.set_window_icon(root)
        if args.mode == "host":
            self.open_host(with_back=False)
        elif args.mode == "client":
            self.open_client(with_back=False)
        else:
            self.open_start()

    def _clear(self):
        if self.screen is not None and hasattr(self.screen, "destroy"):
            self.screen.destroy()
        self.screen = None
        for child in self.root.winfo_children():
            child.destroy()
        self.root.unbind("<Return>")
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        if sys.platform == "darwin":
            self.root.createcommand("::tk::mac::Quit", self.root.destroy)

    def open_start(self):
        self._clear()
        self.root.title("HopGrid")
        self.root.minsize(480, 300)
        self.root.after(10, lambda: self.root.tk.call("wm", "geometry", self.root._w, ""))
        self.screen = StartScreen(self.root, self.open_host, self.open_client)

    def open_host(self, with_back=True):
        import host  # lädt OpenCV/mss/pynput erst, wenn sie gebraucht werden
        self._clear()
        self.root.minsize(540, 300)  # Höhe passt sich dem Inhalt an
        self.screen = host.HostWindow(self.root, self.args, autostart_launch=self.args.autostart,
                                      on_back=self.open_start if with_back else None)

    def open_client(self, with_back=True):
        import client
        self._clear()
        self.root.minsize(500, 300)  # Höhe passt sich dem Inhalt an
        self.screen = client.ClientApp(self.root, self.args,
                                       on_back=self.open_start if with_back else None)


class StartScreen:
    def __init__(self, root, on_host, on_client):
        self.frame = ctk.CTkFrame(root, fg_color=ui.BG)
        self.frame.pack(fill="both", expand=True)
        ui.header(self.frame, "HopGrid", "Fernzugriff im lokalen Netz – verschlüsselt, ohne Cloud")

        body = ctk.CTkFrame(self.frame, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=ui.S4, pady=(ui.S2, ui.S4))
        ui.label(body, "Was möchtest du tun?", "h2").pack(anchor="w", pady=(0, ui.S3))
        host_card = ui.ChoiceCard(body, "share", "Diesen Rechner freigeben",
                                  "Jemand anderes darf diesen Bildschirm sehen und steuern.",
                                  on_host).pack(fill="x", pady=(0, ui.S3))
        client_card = ui.ChoiceCard(body, "connect", "Mit anderem Rechner verbinden",
                                    "Einen freigegebenen Rechner im Netz sehen und steuern.",
                                    on_client).pack(fill="x")
        host_card.make_focusable()
        client_card.make_focusable()

    def destroy(self):
        self.frame.destroy()


def main():
    enable_dpi_awareness()  # vor dem ersten Fenster, sonst unscharf und Maus-Versatz
    args = parse_args()
    ui.setup()
    root = ctk.CTk()
    App(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()

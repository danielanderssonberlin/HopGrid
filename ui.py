"""
HopGrid – gemeinsames Design (customtkinter).

Farben kommen aus dem App-Icon (Navy + Amber) und sind jeweils als Paar
(hell, dunkel) angegeben; customtkinter folgt automatisch dem System-Modus.
Alle Text/Hintergrund-Paare erfüllen mindestens 4,5:1 Kontrast.
"""

import os
import sys
import tkinter as tk

import customtkinter as ctk
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Farben (hell, dunkel)
# ---------------------------------------------------------------------------
BG = ("#F4F6FA", "#0F1622")
CARD = ("#FFFFFF", "#18212F")
CARD_HOVER = ("#EEF3FB", "#1F2A3B")
BORDER = ("#DCE2EC", "#2A3546")
BORDER_FOCUS = ("#1D4E89", "#5B95E5")
TEXT = ("#0F1B2D", "#E8EDF5")
MUTED = ("#5A6678", "#9AA6B8")
PRIMARY = ("#1D4E89", "#2F6BC0")
PRIMARY_HOVER = ("#163E6E", "#2659A3")
SECONDARY = ("#E8EDF5", "#243044")
SECONDARY_HOVER = ("#DCE3EE", "#2C3A52")
ACCENT = "#FFB02E"
SUCCESS = ("#1B7F3B", "#4CC77A")
SUCCESS_BG = ("#E3F4E8", "#163020")
DANGER = ("#B42318", "#F97066")
DANGER_BG = ("#FDECEA", "#3A1D1B")
DANGER_HOVER = ("#F9D9D5", "#4A2422")
NEUTRAL_BG = ("#EEF1F6", "#222C3B")
WARN_BG = ("#FFF4DB", "#3A2E12")
WARN_TEXT = ("#7A4B00", "#FFD27A")
ENTRY_BG = ("#FFFFFF", "#111925")

# Abstände (8er-Raster)
S1, S2, S3, S4 = 4, 8, 16, 24
RADIUS = 10


def create_root():
    """Hauptfenster; mit Drag & Drop (tkinterdnd2), falls verfügbar."""
    try:
        from tkinterdnd2 import TkinterDnD

        class DnDRoot(ctk.CTk, TkinterDnD.DnDWrapper):
            def __init__(self):
                super().__init__()
                self.TkdndVersion = TkinterDnD._require(self)

        root = DnDRoot()
        root.dnd_available = True
    except Exception as exc:
        print(f"Drag & Drop nicht verfügbar: {exc}", file=sys.stderr)
        root = ctk.CTk()
        root.dnd_available = False
    return root


def setup():
    ctk.set_appearance_mode(os.environ.get("HOPGRID_APPEARANCE", "system"))
    ctk.set_default_color_theme("blue")


def resource_path(relative):
    """Pfad zu mitgelieferten Dateien, auch in der gebauten App."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def set_window_icon(root):
    try:
        if sys.platform == "win32":
            root.iconbitmap(resource_path(os.path.join("assets", "icon.ico")))
        else:
            root._hopgrid_icon = tk.PhotoImage(file=resource_path(os.path.join("assets", "icon.png")))
            root.iconphoto(True, root._hopgrid_icon)
    except Exception:
        pass


_fonts = {}


def font(kind):
    """Systemschrift (SF auf macOS, Segoe UI auf Windows) in festen Stufen."""
    if kind not in _fonts:
        size, weight = {
            "title": (22, "bold"), "h2": (16, "bold"), "h3": (14, "bold"),
            "body": (13, "normal"), "small": (12, "normal"), "button": (13, "bold"),
            "mono": (12, "normal"),
        }[kind]
        family = ("Menlo" if sys.platform == "darwin" else "Consolas") if kind == "mono" else None
        _fonts[kind] = ctk.CTkFont(family=family, size=size, weight=weight) if family \
            else ctk.CTkFont(size=size, weight=weight)
    return _fonts[kind]


# ---------------------------------------------------------------------------
# Icons (Linien-Icons, zur Laufzeit gezeichnet – keine Emojis)
# ---------------------------------------------------------------------------
def _draw_icon(kind, color, size=64):
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = max(2, s // 16)
    # Monitor
    d.rounded_rectangle([s * .10, s * .16, s * .90, s * .70], radius=s * .07, outline=color, width=w)
    d.line([s * .50, s * .70, s * .50, s * .82], fill=color, width=w)
    d.line([s * .32, s * .84, s * .68, s * .84], fill=color, width=w)
    if kind == "share":      # Pfeil nach außen
        d.line([s * .36, s * .54, s * .62, s * .30], fill=color, width=w)
        d.line([s * .46, s * .30, s * .63, s * .30], fill=color, width=w)
        d.line([s * .62, s * .29, s * .62, s * .46], fill=color, width=w)
    elif kind == "connect":  # Mauszeiger
        pts = [(s * .40, s * .27), (s * .40, s * .58), (s * .48, s * .50),
               (s * .54, s * .62), (s * .59, s * .60), (s * .53, s * .48), (s * .64, s * .48)]
        d.polygon(pts, outline=color, width=w)
    return img.resize((size, size), Image.LANCZOS)


def icon(kind, size=40):
    light = _draw_icon(kind, PRIMARY[0])
    dark = _draw_icon(kind, "#8DB6EE")
    return ctk.CTkImage(light_image=light, dark_image=dark, size=(size, size))


def app_logo(size=36):
    try:
        img = Image.open(resource_path(os.path.join("assets", "icon.png")))
        return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Bausteine
# ---------------------------------------------------------------------------
def set_cursor(widget, cursor):
    """Zeiger für ein Widget samt aller inneren Tk-Widgets setzen."""
    try:
        widget.tk.call(widget._w, "configure", "-cursor", cursor)
    except tk.TclError:
        pass
    for child in widget.winfo_children():
        set_cursor(child, cursor)


HAND = "pointinghand" if sys.platform == "darwin" else "hand2"


def card(parent, **kw):
    return ctk.CTkFrame(parent, fg_color=CARD, border_color=BORDER, border_width=1,
                        corner_radius=RADIUS, **kw)


def primary_button(parent, text, command, **kw):
    kw.setdefault("height", 40)
    return ctk.CTkButton(parent, text=text, command=command, corner_radius=8,
                         fg_color=PRIMARY, hover_color=PRIMARY_HOVER, text_color="#FFFFFF",
                         font=font("button"), **kw)


def secondary_button(parent, text, command, **kw):
    kw.setdefault("height", 36)
    return ctk.CTkButton(parent, text=text, command=command, corner_radius=8,
                         fg_color=SECONDARY, hover_color=SECONDARY_HOVER, text_color=TEXT,
                         font=font("button"), **kw)


def danger_button(parent, text, command, **kw):
    kw.setdefault("height", 40)
    return ctk.CTkButton(parent, text=text, command=command, corner_radius=8,
                         fg_color=DANGER_BG, hover_color=DANGER_HOVER, text_color=DANGER,
                         font=font("button"), **kw)


def checkbox(parent, text, variable, command=None):
    return ctk.CTkCheckBox(parent, text=text, variable=variable, command=command,
                           checkbox_width=20, checkbox_height=20, corner_radius=5,
                           border_width=2, fg_color=PRIMARY, hover_color=PRIMARY_HOVER,
                           border_color=MUTED, text_color=TEXT, font=font("body"))


def label(parent, text="", kind="body", color=TEXT, **kw):
    return ctk.CTkLabel(parent, text=text, font=font(kind), text_color=color, **kw)


class Field:
    """Beschriftetes Eingabefeld mit Fehlertext direkt darunter."""

    def __init__(self, parent, title, value="", secret=False, hint="", width=0, placeholder="",
                 wrap=440):
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        label(self.frame, title, "small", MUTED).pack(anchor="w")
        self.entry = ctk.CTkEntry(self.frame, height=36, corner_radius=8, border_width=1,
                                  border_color=BORDER, fg_color=ENTRY_BG, text_color=TEXT,
                                  font=font("body"), show="•" if secret else "",
                                  placeholder_text=placeholder or None,
                                  placeholder_text_color=MUTED,
                                  **({"width": width} if width else {}))
        self.entry.pack(fill="x", pady=(S1, 0))
        if value:
            self.entry.insert(0, value)
        self.message = label(self.frame, hint, "small", MUTED, anchor="w", justify="left",
                             wraplength=wrap)
        self._hint = hint
        if hint:
            self.message.pack(anchor="w", fill="x")
        self.entry.bind("<FocusIn>", lambda _e: self.entry.configure(border_color=BORDER_FOCUS))
        self.entry.bind("<FocusOut>", lambda _e: self._reset_border())
        self.entry.bind("<Key>", lambda _e: self.clear_error(), add="+")
        self._error = False

    def pack(self, **kw):
        self.frame.pack(**kw)
        return self

    def grid(self, **kw):
        self.frame.grid(**kw)
        return self

    def get(self):
        return self.entry.get()

    def set(self, value):
        self.entry.delete(0, "end")
        if value:
            self.entry.insert(0, value)
        else:
            self.entry._activate_placeholder()  # Platzhalter wieder anzeigen

    def set_placeholder(self, text):
        self.entry.configure(placeholder_text=text or None)
        if not self.entry.get():
            self.entry._activate_placeholder()

    def set_hint(self, hint):
        self._hint = hint
        if not self._error:
            self._show(hint, MUTED)

    def _show(self, text, color):
        self.message.configure(text=text, text_color=color)
        if text:
            self.message.pack(anchor="w", fill="x")
        else:
            self.message.pack_forget()

    def _reset_border(self):
        self.entry.configure(border_color=DANGER if self._error else BORDER)

    def error(self, text):
        self._error = True
        self.entry.configure(border_color=DANGER)
        self._show(text, DANGER)
        self.entry.focus_set()

    def clear_error(self):
        if self._error:
            self._error = False
            self.entry.configure(border_color=BORDER_FOCUS)
            self._show(self._hint, MUTED)

    def set_state(self, enabled):
        self.entry.configure(state="normal" if enabled else "disabled",
                             text_color=TEXT if enabled else MUTED)


class Badge:
    """Status-Pille: Punkt + Text, Farbe je Zustand (nicht nur Farbe: Text sagt es auch)."""

    STYLES = {
        "idle": (NEUTRAL_BG, MUTED),
        "active": (SUCCESS_BG, SUCCESS),
        "busy": (WARN_BG, WARN_TEXT),
        "error": (DANGER_BG, DANGER),
    }

    def __init__(self, parent):
        self.widget = ctk.CTkLabel(parent, text="", corner_radius=12, height=26,
                                   font=font("small"), padx=12)

    def set(self, state, text):
        bg, fg = self.STYLES[state]
        self.widget.configure(text=f"●  {text}", fg_color=bg, text_color=fg)


def header(parent, title, subtitle="", on_back=None):
    """Kopfzeile: optional Zurück, Logo, Titel, Untertitel."""
    bar = ctk.CTkFrame(parent, fg_color="transparent")
    bar.pack(fill="x", padx=S4, pady=(S3, S3))
    row = ctk.CTkFrame(bar, fg_color="transparent")
    row.pack(fill="x")
    if on_back is not None:
        # Kompakt: Zurück steht in derselben Zeile wie der Titel (statt des Logos)
        back = ctk.CTkButton(row, text="‹ Zurück", command=on_back, width=84, height=32,
                             corner_radius=8, fg_color=SECONDARY, hover_color=SECONDARY_HOVER,
                             text_color=TEXT, font=font("small"))
        back.pack(side="left", padx=(0, 14), anchor="n", pady=(2, 0))
        bar.back_button = back
    else:
        logo = app_logo(36)
        if logo is not None:
            ctk.CTkLabel(row, image=logo, text="").pack(side="left", padx=(0, 12))
    texts = ctk.CTkFrame(row, fg_color="transparent")
    texts.pack(side="left", fill="x", expand=True)
    label(texts, title, "title").pack(anchor="w")
    if subtitle:
        label(texts, subtitle, "small", MUTED, anchor="w", justify="left").pack(anchor="w")
    return bar


class ChoiceCard:
    """Große, ganz klickbare Auswahlkarte mit Icon, Titel, Beschreibung und Pfeil.
    Per Tab erreichbar, Enter/Leertaste lösen aus."""

    def __init__(self, parent, icon_kind, title, text, command):
        self.command = command
        self.frame = card(parent, height=96)
        self.frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self.frame, image=icon(icon_kind, 40), text="").grid(
            row=0, column=0, rowspan=2, padx=(S3 + 4, S3), pady=S3)
        label(self.frame, title, "h3", anchor="w").grid(row=0, column=1, sticky="sw",
                                                       pady=(S3, 0))
        label(self.frame, text, "small", MUTED, anchor="w", justify="left",
              wraplength=300).grid(row=1, column=1, sticky="nw", pady=(2, S3))
        label(self.frame, "›", "title", MUTED).grid(row=0, column=2, rowspan=2, padx=(S2, S3 + 4))

        self._bind_all(self.frame)
        set_cursor(self.frame, HAND)
        self.frame.bind("<FocusIn>", lambda _e: self.frame.configure(border_color=BORDER_FOCUS))
        self.frame.bind("<FocusOut>", lambda _e: self.frame.configure(border_color=BORDER))

    def _bind_all(self, widget):
        widget.bind("<Button-1>", lambda _e: self.command(), add="+")
        widget.bind("<Enter>", lambda _e: self.frame.configure(fg_color=CARD_HOVER), add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        for child in widget.winfo_children():
            self._bind_all(child)

    def _on_leave(self, _event):
        x, y = self.frame.winfo_pointerxy()
        inside = self.frame.winfo_containing(x, y)
        if inside is None or not str(inside).startswith(str(self.frame)):
            self.frame.configure(fg_color=CARD)

    def make_focusable(self):
        """Tastatur: Fokus per Tab, auslösen mit Enter oder Leertaste."""
        self.frame.tk.call(self.frame._w, "configure", "-takefocus", 1)
        self.frame.bind("<Return>", lambda _e: self.command())
        self.frame.bind("<space>", lambda _e: self.command())

    def pack(self, **kw):
        self.frame.pack(**kw)
        return self


class Collapsible:
    """Ein-/ausklappbarer Bereich (z. B. 'Erweitert', 'Protokoll')."""

    def __init__(self, parent, title, open_=False, on_toggle=None):
        self.outer = ctk.CTkFrame(parent, fg_color="transparent")
        self.title = title
        self.on_toggle = on_toggle
        self.button = ctk.CTkButton(self.outer, text="", command=self.toggle, height=28,
                                    fg_color="transparent", hover_color=SECONDARY,
                                    text_color=MUTED, font=font("small"), anchor="w")
        self.button.pack(anchor="w")
        self.body = ctk.CTkFrame(self.outer, fg_color="transparent")
        self.is_open = open_
        self._render()

    def _render(self):
        self.button.configure(text=("▾  " if self.is_open else "▸  ") + self.title)
        if self.is_open:
            self.body.pack(fill="both", expand=True, pady=(S1, 0))
        else:
            self.body.pack_forget()

    def toggle(self):
        self.is_open = not self.is_open
        self._render()
        if self.on_toggle is not None:
            self.on_toggle()

    def pack(self, **kw):
        self.outer.pack(**kw)
        return self


class Banner:
    """Hinweisleiste über dem Inhalt (z. B. 'Steuerung blockiert', 'Verbinde neu …')."""

    def __init__(self, parent):
        self.parent = parent
        self.widget = None

    def show(self, text, kind="warn", rely=0.0, anchor="n", y=12):
        bg, fg = {"warn": (WARN_BG, WARN_TEXT), "info": (CARD, TEXT),
                  "error": (DANGER_BG, DANGER)}[kind]
        if self.widget is None:
            self.widget = ctk.CTkLabel(self.parent, text="", corner_radius=10, padx=18, pady=10,
                                       font=font("h3"), wraplength=560, justify="left")
        self.widget.configure(text=text, fg_color=bg, text_color=fg)
        self.widget.place(relx=0.5, rely=rely, y=y, anchor=anchor)
        self.widget.lift()

    def hide(self):
        if self.widget is not None:
            self.widget.destroy()
            self.widget = None

    @property
    def visible(self):
        return self.widget is not None


class TransferBar:
    """Leiste unten im Verbindungsfenster für Dateiübertragungen.

    Arten: 'progress' (mit Balken), 'success', 'error', 'offer' (mit Knopf),
    'received' (Im Finder/Explorer zeigen + Griff zum Herausziehen)."""

    def __init__(self, root):
        self.root = root
        self.frame = None
        self.kind = None
        self._hide_job = None
        self._progress = None
        self._label = None

    def show(self, text, kind, fraction=None, action=None, files=None, hide_after=None):
        if self._hide_job is not None:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None
        # Fortschritt nur aktualisieren statt neu aufbauen (kein Flackern)
        if kind == "progress" and self.kind == "progress" and self.frame is not None:
            self._label.configure(text=text)
            if fraction is not None:
                self._progress.set(max(0.0, min(1.0, fraction)))
            return
        self.hide()
        self.kind = kind
        color = {"error": DANGER, "success": SUCCESS}.get(kind, TEXT)
        self.frame = ctk.CTkFrame(self.root, fg_color=CARD, border_color=BORDER, border_width=1,
                                  corner_radius=12)
        row = ctk.CTkFrame(self.frame, fg_color="transparent")
        row.pack(fill="x", padx=S3, pady=(12, 12 if kind != "progress" else 6))
        wrap = max(260, min(560, self.root.winfo_width() - 330))
        self._label = label(row, text, "body", color, anchor="w", justify="left", wraplength=wrap)
        self._label.pack(side="left", fill="x", expand=True)
        if kind == "progress":
            self._progress = ctk.CTkProgressBar(self.frame, height=6, corner_radius=3,
                                                progress_color=PRIMARY, fg_color=NEUTRAL_BG)
            self._progress.set(fraction or 0.0)
            self._progress.pack(fill="x", padx=S3, pady=(0, 12))
        else:
            ctk.CTkButton(row, text="✕", width=30, height=30, corner_radius=8,
                          fg_color="transparent", hover_color=SECONDARY, text_color=MUTED,
                          command=self.hide).pack(side="right", padx=(S2, 0))
        if kind == "offer" and action is not None:
            primary_button(row, action[0], action[1], height=32).pack(side="right", padx=(S2, 0))
        if kind == "received" and files:
            import filetransfer
            where = "Im Finder zeigen" if sys.platform == "darwin" else "Im Explorer zeigen"
            secondary_button(row, where, lambda: filetransfer.reveal(files), height=32
                             ).pack(side="right", padx=(S2, 0))
            self._drag_handle(row, files)
        self.frame.place(relx=0.5, rely=1.0, y=-16, anchor="s")
        self.frame.lift()
        if hide_after:
            self._hide_job = self.root.after(hide_after, self.hide)

    def _drag_handle(self, parent, files):
        """Griff, mit dem man die empfangenen Dateien aus dem Fenster ziehen kann."""
        if not getattr(self.root, "dnd_available", False):
            return
        from tkinterdnd2 import COPY, DND_FILES
        handle = ctk.CTkLabel(parent, text="⠿  Herausziehen", corner_radius=8, height=32,
                              fg_color=SECONDARY, text_color=TEXT, font=font("button"), padx=12)
        handle.pack(side="right", padx=(S2, 0))

        def init_drag(_event):
            return (COPY, DND_FILES, tuple(files))

        for widget in (handle, *handle.winfo_children()):
            try:
                widget.drag_source_register(1, DND_FILES)
                widget.dnd_bind("<<DragInitCmd>>", init_drag)
            except Exception:
                pass
        set_cursor(handle, HAND)

    def hide(self):
        if self.frame is not None:
            self.frame.destroy()
        self.frame = None
        self.kind = None
        self._progress = None
        self._label = None

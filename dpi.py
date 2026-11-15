"""Windows: Prozess DPI-aware machen.

Ohne das streckt Windows bei 125 %/150 % Skalierung das ganze Fenster (unscharf),
und Bildschirmaufnahme und Mauskoordinaten passen nicht zusammen.
Muss aufgerufen werden, BEVOR ein Fenster entsteht. Mehrfacher Aufruf ist harmlos."""

import sys


def enable_dpi_awareness():
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

# HopGrid

LAN-Remote-Desktop (macOS/Windows), Python. Gebaut 15.11.2026.

- Eine App: `hopgrid.py` (Auswahl Host/Client, `--mode`, `--autostart`), `host.py` (HostWindow, `--cli`), `client.py`, `protocol.py` (Login + Verschlüsselung), `autostart.py` (Run-Key/LaunchAgent, host.json mit Salt+Schlüssel, 600)
- Krypto: X25519 je Verbindung, HKDF über ECDH + PBKDF2-Passwortschlüssel, AES-256-GCM mit Zähler-Nonce je Richtung, gegenseitiger HMAC-Beweis über Transkript
- Build: `bash build_mac.sh` → dist/HopGrid.app + ZIP (ad-hoc signiert, nach jedem Neubau macOS-Freigaben neu erteilen). `.exe` nur auf Windows: `build_windows.bat` oder GitHub Actions (`.github/workflows/build.yml`, Tag `v*` → Release)
- Alias: UDP-Broadcast-Suche auf Port 48080 (`proto.discover`), Host antwortet mit Alias+Port. Client merkt sich Host/Port/User in App-Support/%APPDATA% HopGrid/client.json
- Repo: https://github.com/danielanderssonberlin/HopGrid (öffentlich). Lizenz noch offen. Actions-Build für Windows/macOS arm64/x86_64 läuft grün
- Getestet: Login, Fehllogin, gefälschter Host, Manipulation, Frame-Übertragung (Loopback). Noch nicht getestet: echte Steuerung zwischen zwei Rechnern, Windows-Build
- Gatekeeper: ohne Apple Developer ID (99 $/Jahr) muss jeder Mac-Nutzer manuell "Trotzdem öffnen". Workflow signiert+notarisiert automatisch, sobald Secrets gesetzt sind (Daniel entscheidet)
- v0.3.0: Client-Passwort per keyring im System-Schlüsselbund (Backend fest gesetzt, sonst in PyInstaller nicht gefunden), Bild 2560px/Q85/JPEG 4:4:4, DPI-Awareness in hopgrid.py vor Tk (Bugfix), Pfeil-Cursor. Kein Apple-Developer-Konto (Daniel, 15.11.2026) → Gatekeeper bleibt. v0.1.0 gelöscht
- v0.4.0: Protokoll v3 (MSG_PING, MSG_CLIPBOARD, Versionsfeld). Host: Aufnahmefehler beenden Sitzung nicht, neuer Login übernimmt alte Sitzung, SESSION_TIMEOUT 20s, KeepAwake (macOS NSProcessInfo-Activity, Windows SetThreadExecutionState), Schließen-Dialog (Windows pystray-Tray, macOS Dock), host.json mit remember. Client: Neuverbinden 120s, Rückfrage bei Cmd+Q, Zwischenablage (Tk) + Host pyperclip. Tests im Scratchpad: loop/alias/robust/clip_test.py
- In Arbeit nach v0.4.0 (noch nicht gebaut): neues Design mit customtkinter 5.2.2 (`ui.py`, Navy/Amber, hell/dunkel), Zurück-Navigation, fremder Cursor (MSG_CURSOR), Hinweise vom Host (MSG_NOTICE: macOS AX fehlt, Windows Admin-Fenster, Sperrbildschirm), MacKeyboard über Quartz statt pynput (TIS-Hänger), Schließen fragt immer + Auswahl merken, Passwort-Platzhalter "Gespeichert", Client lädt Schlüsselbund-Passwort im Hintergrund (macOS-Abfrage blockierte Fenster). Screenshot-Tests: scratchpad shot.py / session_shot.py mit HOME=scratchpad/home

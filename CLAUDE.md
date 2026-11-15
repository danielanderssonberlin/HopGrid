# HopGrid

LAN-Remote-Desktop (macOS/Windows), Python. Gebaut 15.11.2026.

- Eine App: `hopgrid.py` (Auswahl Host/Client, `--mode`, `--autostart`), `host.py` (HostWindow, `--cli`), `client.py`, `protocol.py` (Login + Verschlüsselung), `autostart.py` (Run-Key/LaunchAgent, host.json mit Salt+Schlüssel, 600)
- Krypto: X25519 je Verbindung, HKDF über ECDH + PBKDF2-Passwortschlüssel, AES-256-GCM mit Zähler-Nonce je Richtung, gegenseitiger HMAC-Beweis über Transkript
- Build: `bash build_mac.sh` → dist/HopGrid.app + ZIP (ad-hoc signiert, nach jedem Neubau macOS-Freigaben neu erteilen). `.exe` nur auf Windows: `build_windows.bat` oder GitHub Actions (`.github/workflows/build.yml`, Tag `v*` → Release)
- Alias: UDP-Broadcast-Suche auf Port 48080 (`proto.discover`), Host antwortet mit Alias+Port. Client merkt sich Host/Port/User in App-Support/%APPDATA% HopGrid/client.json
- Repo: https://github.com/danielanderssonberlin/HopGrid (öffentlich). Lizenz noch offen. Actions-Build für Windows/macOS arm64/x86_64 läuft grün
- Getestet: Login, Fehllogin, gefälschter Host, Manipulation, Frame-Übertragung (Loopback). Noch nicht getestet: echte Steuerung zwischen zwei Rechnern, Windows-Build
- Gatekeeper: ohne Apple Developer ID (99 $/Jahr) muss jeder Mac-Nutzer manuell "Trotzdem öffnen". Workflow signiert+notarisiert automatisch, sobald Secrets gesetzt sind (Daniel entscheidet)

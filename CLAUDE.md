# HopGrid

LAN-Remote-Desktop (macOS/Windows), Python. Gebaut 15.11.2026.

- Dateien: `protocol.py` (Login + Verschlüsselung), `host.py` (Tk-Fenster, `--cli` für Terminal), `client.py`
- Krypto: X25519 je Verbindung, HKDF über ECDH + PBKDF2-Passwortschlüssel, AES-256-GCM mit Zähler-Nonce je Richtung, gegenseitiger HMAC-Beweis über Transkript
- Build: `bash build_mac.sh` → dist/*.app + ZIPs (ad-hoc signiert, nach jedem Neubau macOS-Freigaben neu erteilen). `.exe` nur auf Windows: `build_windows.bat` oder GitHub Actions (`.github/workflows/build.yml`, Tag `v*` → Release)
- Alias: UDP-Broadcast-Suche auf Port 48080 (`proto.discover`), Host antwortet mit Alias+Port. Client merkt sich Host/Port/User in App-Support/%APPDATA% HopGrid/client.json
- Repo: https://github.com/danielanderssonberlin/HopGrid (öffentlich). Lizenz noch offen. Actions-Build für Windows/macOS arm64/x86_64 läuft grün
- Getestet: Login, Fehllogin, gefälschter Host, Manipulation, Frame-Übertragung (Loopback). Noch nicht getestet: echte Steuerung zwischen zwei Rechnern, Windows-Build

# HopGrid

Schlanker Fernzugriff auf einen anderen Rechner **im eigenen lokalen Netz**, für macOS und Windows.
Ohne Cloud, ohne Konto, ohne externen Server: Der Client verbindet sich direkt mit der IP-Adresse des Hosts.
Die Verbindung ist Ende-zu-Ende verschlüsselt.

- **HopGrid Host** läuft auf dem Rechner, der gesteuert wird. Er teilt den Bildschirm und führt Maus und Tastatur aus.
- **HopGrid Client** läuft auf dem Rechner, an dem du sitzt. Er zeigt den fremden Bildschirm und überträgt deine Eingaben.

## Download

Fertige Programme gibt es unter [Releases](../../releases):

| System | Host | Client |
|---|---|---|
| Windows | `HopGrid-Host.exe` | `HopGrid-Client.exe` |
| macOS Apple Silicon | `HopGrid-Host-macOS-arm64.zip` | `HopGrid-Client-macOS-arm64.zip` |
| macOS Intel | `HopGrid-Host-macOS-x86_64.zip` | `HopGrid-Client-macOS-x86_64.zip` |

Die Programme sind nicht von Apple oder Microsoft signiert. Beim ersten Start warnt das System deshalb:

- **macOS:** Rechtsklick auf die App > *Öffnen* > *Öffnen*. Alternativ im Terminal: `xattr -dr com.apple.quarantine "HopGrid Host.app"`
- **Windows:** SmartScreen meldet „Der Computer wurde durch Windows geschützt“. Dann *Weitere Informationen* > *Trotzdem ausführen*.

## Benutzung

1. Auf dem zu steuernden Rechner **HopGrid Host** starten. Benutzername und Passwort (mindestens 8 Zeichen) vergeben und *Freigabe starten* klicken. Das Fenster zeigt dann die Adresse an, zum Beispiel `192.168.1.20:8080`.
2. Auf dem anderen Rechner **HopGrid Client** starten. IP-Adresse, Port, Benutzername und Passwort eingeben und *Verbinden* klicken.
3. Das Fenster zeigt den Bildschirm des Hosts. Maus, Scrollrad und Tastatur wirken auf dem Host.

Mit *Freigabe stoppen* oder durch Schließen des Host-Fensters endet die Verbindung sofort.

## Einrichtung auf dem Host

### macOS

Der Host braucht zwei Freigaben unter **Systemeinstellungen > Datenschutz & Sicherheit**:

- **Bildschirm- & Systemaudioaufnahme:** *HopGrid Host* aktivieren. Ohne diese Freigabe sieht der Client nur den Schreibtischhintergrund.
- **Bedienungshilfen:** *HopGrid Host* aktivieren. Ohne diese Freigabe passieren keine Klicks und keine Tastatureingaben.

Beim ersten Klick auf *Freigabe starten* fragt macOS selbst nach beiden Freigaben. Danach die App neu starten.

### Windows

- Beim ersten Start fragt die Windows-Firewall nach. Nur **Private Netzwerke** erlauben.
- Fenster, die als Administrator laufen (zum Beispiel der Task-Manager), und UAC-Abfragen nehmen nur dann Eingaben an, wenn auch der Host als Administrator läuft. Den Sperrbildschirm kann HopGrid nicht übertragen.

## Sicherheit

- **Verschlüsselung:** Nach dem Login sind alle Bilder und Eingaben mit AES-256-GCM verschlüsselt. Jede Verbindung bekommt per X25519 einen neuen Schlüssel.
- **Login:** Das Passwort wird nie übertragen. Beide Seiten beweisen per HMAC, dass sie es kennen. Ein gefälschter Host im Netz fällt dadurch auf, und der Client bricht ab.
- **Schlüssel:** Der Sitzungsschlüssel hängt zusätzlich vom Passwort ab (PBKDF2-SHA256, 200.000 Runden). Wer das Passwort nicht kennt, kann den Datenverkehr nicht mitlesen, auch nicht als Mittelsmann.
- **Schutz vor Raten:** Nach jedem Fehlversuch wartet der Host 2 Sekunden. Nach 5 Fehlversuchen ist die IP-Adresse für 5 Minuten gesperrt.
- **Nur LAN:** Der Host nimmt nur Verbindungen aus privaten Adressbereichen an. Es kann immer nur ein Client verbunden sein.

Grenzen, die du kennen solltest:

- Die Sicherheit hängt am Passwort. Wer im Netz einen Host vortäuscht, könnte ein schwaches Passwort offline durchprobieren. Nimm deshalb ein langes Passwort.
- Der Host gehört nicht ins Internet. Gib den Port nicht im Router frei. Für Zugriff von unterwegs ist ein VPN der richtige Weg, zum Beispiel WireGuard oder Tailscale.
- Der Code wurde nicht von unabhängiger Seite geprüft.

## Grenzen

- Es wird ein Monitor übertragen. Welcher, legt `--monitor N` fest.
- Der Mauszeiger des Hosts ist im Bild nicht zu sehen. Man sieht den eigenen Zeiger über dem Bild.
- Beide Rechner sollten dasselbe Tastaturlayout haben, sonst kommen Sonderzeichen falsch an.
- Tastenkürzel des Systems wie Cmd+Tab oder die Windows-Taste bleiben auf dem eigenen Rechner.
- Wenn ein Mac einen Windows-Rechner steuert, den Client mit `--cmd-as-ctrl` starten. Dann wird Cmd+C zu Strg+C.

## Aus dem Quellcode starten

Voraussetzung ist Python 3.10 oder neuer.

```bash
pip install -r requirements.txt
python host.py           # Fenster; mit --cli im Terminal
python client.py --host 192.168.1.20
```

Optionen für den Host: `--port 8080 --fps 15 --quality 70 --max-width 1600 --monitor 1`

## Selbst bauen

- **macOS:** `bash build_mac.sh` erzeugt `dist/HopGrid Host.app` und `dist/HopGrid Client.app`.
- **Windows:** `build_windows.bat` erzeugt `dist\HopGrid-Host.exe` und `dist\HopGrid-Client.exe`.
- **GitHub Actions:** Ein Tag `v*` baut alle Varianten und legt sie als Release an.

PyInstaller kann nicht für ein anderes System bauen. Die `.exe` entsteht deshalb auf Windows, die `.app` auf macOS.

## Aufbau

| Datei | Inhalt |
|---|---|
| `protocol.py` | Login, Schlüsselaustausch, verschlüsselter Kanal |
| `host.py` | Bildschirmaufnahme (mss und OpenCV als JPEG), Eingaben (pynput, auf macOS Quartz), Host-Fenster |
| `client.py` | Login-Fenster, Anzeige (Tkinter und Pillow), Weitergabe der Eingaben |

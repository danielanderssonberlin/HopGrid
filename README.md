# HopGrid

Schlanker Fernzugriff auf einen anderen Rechner **im eigenen lokalen Netz**, für macOS und Windows.
Ohne Cloud, ohne Konto, ohne externen Server: Der Client verbindet sich direkt mit dem Host, per IP-Adresse oder über einen frei wählbaren Namen (Alias).
Die Verbindung ist Ende-zu-Ende verschlüsselt.

Eine App für beide Seiten. Beim Start wählst du:

- **Diesen Rechner freigeben** (Host): auf dem Rechner, der gesteuert wird. Er teilt den Bildschirm und führt Maus und Tastatur aus.
- **Mit anderem Rechner verbinden** (Client): auf dem Rechner, an dem du sitzt. Er zeigt den fremden Bildschirm und überträgt deine Eingaben.

## Download

Fertige Programme gibt es unter [Releases](../../releases):

| System | Datei |
|---|---|
| Windows | `HopGrid.exe` |
| macOS Apple Silicon (M1 und neuer) | `HopGrid-macOS-arm64.zip` |
| macOS Intel | `HopGrid-macOS-x86_64.zip` |

Die Programme sind noch nicht von Apple oder Microsoft signiert. Beim ersten Start blockt das System sie deshalb:

- **macOS:** Die App einmal öffnen. Dann *Systemeinstellungen > Datenschutz & Sicherheit*, nach unten scrollen und bei „HopGrid wurde blockiert“ auf *Trotzdem öffnen* klicken. Seit macOS 15 geht das nur noch so, Rechtsklick > *Öffnen* reicht nicht mehr. Alternativ im Terminal: `xattr -dr com.apple.quarantine /Applications/HopGrid.app`
- **Windows:** SmartScreen meldet „Der Computer wurde durch Windows geschützt“. Dann *Weitere Informationen* > *Trotzdem ausführen*.

## Benutzung

1. Auf dem zu steuernden Rechner HopGrid starten und **Diesen Rechner freigeben** wählen. Einen **Alias** wählen (zum Beispiel `Büro-PC`, vorbelegt ist der Rechnername), Benutzername und Passwort (mindestens 8 Zeichen) vergeben und *Freigabe starten* klicken.
2. Auf dem anderen Rechner HopGrid starten und **Mit anderem Rechner verbinden** wählen. Im Feld *Host* den Alias oder die IP-Adresse eingeben, dazu Benutzername und Passwort, dann *Verbinden* klicken. *Suchen …* listet alle laufenden Hosts im Netz auf.
3. Das Fenster zeigt den Bildschirm des Hosts. Maus, Scrollrad und Tastatur wirken auf dem Host.

Mit *Freigabe stoppen* endet die Verbindung sofort. Schließt du das Freigabe-Fenster, fragt HopGrid: **im Hintergrund weiterlaufen** (Windows: Symbol unten rechts im Infobereich, macOS: im Dock) oder **ganz beenden**. Mit *Auswahl merken* fragt HopGrid nicht mehr. Unter *Erweitert* > *Beim Schließen wieder fragen* lässt sich das zurücksetzen.

Im Verbindungsfenster siehst du den Mauszeiger des anderen Rechners in seiner aktuellen Form: Pfeil, Textcursor, Doppelpfeil zum Größenändern und weitere. Dein eigener Zeiger übernimmt die Form ebenfalls. Im Menü *Verbindung* lässt sich während der Sitzung umschalten: fremden Zeiger anzeigen, Zeigerform übernehmen, Zwischenablage teilen, Cmd als Strg. Kommt die Steuerung auf dem Host nicht an, zeigt ein gelbes Banner den Grund und die Abhilfe, zum Beispiel fehlende Bedienungshilfen auf dem Mac oder ein Admin-Fenster unter Windows.

### Einstellungen speichern

- **Freigabe:** Alias, Benutzername und Port werden gespeichert. Mit dem Haken *Einstellungen und Zugangsdaten speichern* merkt sich HopGrid auch das Passwort, und zwar nur als abgeleiteten Schlüssel, nicht im Klartext. Die Passwortfelder zeigen dann „Gespeichert“. Lass sie einfach leer.
- **Verbinden:** Host, Port, Benutzername und Optionen werden gespeichert. *Passwort speichern* legt das Passwort im Schlüsselbund des Systems ab (siehe unten).

### Kopieren und Einfügen

Die Zwischenablage wird in beide Richtungen abgeglichen, bei Text bis 1 MB. Kopierst du auf deinem Rechner etwas, kannst du es im HopGrid-Fenster mit Strg+V bzw. Cmd+V einfügen, und umgekehrt. Ausschalten kannst du das mit dem Haken *Zwischenablage teilen*. Bilder und Dateien werden nicht übertragen.

### Dateien und Ordner übertragen

- **Hin zum anderen Rechner:** Dateien oder ganze Ordner auf das Bild des anderen Rechners ziehen und dort loslassen, wo sie hin sollen: auf den Schreibtisch oder in ein Finder- bzw. Explorer-Fenster. Liegt dort etwas anderes, zum Beispiel ein Browser, wird nichts kopiert, und HopGrid sagt dir, warum. Auf dem Mac fragt macOS beim ersten Mal, ob HopGrid den Finder abfragen darf. *Erlauben* wählen.
- **Ohne Ziehen:** Menü *Verbindung* > *Dateien senden …* bzw. *Ordner senden …* legt dort ab, wo der Zeiger auf dem anderen Rechner gerade steht.
- **Zurück zu dir:** Auf dem anderen Rechner Dateien im Finder oder Explorer kopieren (Cmd+C bzw. Strg+C). Unten im HopGrid-Fenster erscheint *Hierher holen*. Danach liegen sie bei dir in *Downloads › HopGrid*. Mit dem Griff *Herausziehen* ziehst du sie direkt an die gewünschte Stelle.
- Ordnerstruktur und leere Ordner bleiben erhalten. Nichts wird überschrieben, bei gleichen Namen hängt HopGrid „(2)“ an. Die Übertragung ist wie alles andere verschlüsselt.
- Eine Datei direkt aus dem *Bild* des anderen Rechners herausziehen geht nicht. Das Bild enthält keine echten Dateien. Dafür ist der Weg über Kopieren da.

### Stabile Verbindung

- Reißt die Verbindung ab, zum Beispiel weil das WLAN kurz weg ist, verbindet der Client bis zu 2 Minuten lang automatisch neu.
- Sperrbildschirm oder Windows-UAC-Abfragen trennen die Verbindung nicht mehr. Das Bild kommt zurück, sobald der Bildschirm wieder lesbar ist.
- Solange die Freigabe läuft, geht der Host nicht in den Ruhezustand. Auf dem Mac wird die App im Hintergrund außerdem nicht gedrosselt (App Nap).
- Cmd+Q und das Schließen des Fensters beenden eine laufende Verbindung erst nach einer Rückfrage.
- Host und Client müssen dieselbe HopGrid-Version haben. Sonst gibt es eine klare Meldung.

### Autostart

Setze in der Freigabe den Haken **Bei der Anmeldung automatisch starten und freigeben**. Dann startet HopGrid bei jeder Anmeldung an Windows oder macOS, gibt sofort frei und legt das Fenster in die Taskleiste bzw. ins Dock.

- **Windows:** Eintrag unter `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run`
- **macOS:** LaunchAgent `~/Library/LaunchAgents/com.kenomedia.hopgrid.plist`

Damit der Start ohne Eingabe klappt, speichert HopGrid Alias, Benutzername, Port und den aus dem Passwort abgeleiteten Schlüssel. Das Passwort selbst wird nicht gespeichert. Die Datei liegt unter `%APPDATA%\HopGrid\host.json` bzw. `~/Library/Application Support/HopGrid/host.json` und ist nur für dein Benutzerkonto lesbar. Wer diese Datei hat, kann sich trotzdem anmelden. Haken entfernen löscht Autostart und Datei.

HopGrid startet bei der **Anmeldung**, nicht schon beim Hochfahren. Den Anmeldebildschirm kann HopGrid nicht übertragen.

### Alias statt IP-Adresse

Der Alias funktioniert auch dann, wenn der Router dem Host eine neue IP-Adresse gibt. Der Client ruft per UDP-Broadcast (Port 48080) ins lokale Netz, und jeder Host antwortet mit seinem Alias und seiner aktuellen Adresse. Ein Server ist dafür nicht nötig. Findet der Client keinen Host mit dem Alias, versucht er den Namen als normalen Rechnernamen aufzulösen, zum Beispiel `macbook.local`.

Die Suche funktioniert nur im selben Netz. Gast-WLANs und getrennte Netze lassen Broadcasts meist nicht durch. Dann hilft nur die IP-Adresse.

Der Client merkt sich Host, Port und Benutzernamen. Mit dem Haken **Passwort speichern** legt er auch das Passwort ab, und zwar im Schlüsselbund des Systems (macOS: Schlüsselbundverwaltung, Windows: Anmeldeinformationsverwaltung), nicht in einer Datei. Haken entfernen und einmal verbinden löscht es wieder.

## Einrichtung auf dem Host

### macOS

Der Host braucht zwei Freigaben unter **Systemeinstellungen > Datenschutz & Sicherheit**:

- **Bildschirm- & Systemaudioaufnahme:** *HopGrid* aktivieren. Ohne diese Freigabe sieht der Client nur den Schreibtischhintergrund.
- **Bedienungshilfen:** *HopGrid* aktivieren. Ohne diese Freigabe passieren keine Klicks und keine Tastatureingaben.

Beim ersten Klick auf *Freigabe starten* fragt macOS selbst nach beiden Freigaben. Danach die App neu starten.

### Windows

- Beim ersten Start fragt die Windows-Firewall nach. Nur **Private Netzwerke** erlauben. Das gilt für die Verbindung (TCP 8080) und die Suche (UDP 48080).
- Fenster, die als Administrator laufen (zum Beispiel der Task-Manager), und UAC-Abfragen nehmen nur dann Eingaben an, wenn auch der Host als Administrator läuft. Den Sperrbildschirm kann HopGrid nicht übertragen.

## Sicherheit

- **Verschlüsselung:** Nach dem Login sind alle Bilder und Eingaben mit AES-256-GCM verschlüsselt. Jede Verbindung bekommt per X25519 einen neuen Schlüssel.
- **Login:** Das Passwort wird nie übertragen. Beide Seiten beweisen per HMAC, dass sie es kennen. Ein gefälschter Host im Netz fällt dadurch auf, und der Client bricht ab.
- **Schlüssel:** Der Sitzungsschlüssel hängt zusätzlich vom Passwort ab (PBKDF2-SHA256, 200.000 Runden). Wer das Passwort nicht kennt, kann den Datenverkehr nicht mitlesen, auch nicht als Mittelsmann.
- **Schutz vor Raten:** Nach jedem Fehlversuch wartet der Host 2 Sekunden. Nach 5 Fehlversuchen ist die IP-Adresse für 5 Minuten gesperrt.
- **Nur LAN:** Der Host nimmt nur Verbindungen aus privaten Adressbereichen an. Es kann immer nur ein Client verbunden sein.
- **Suche:** Die Antwort auf die Suche ist nicht signiert. Ein Gerät im Netz könnte sich also als dein Alias ausgeben. Es scheitert dann aber am Login, weil der Host das Passwort beweisen muss.

Grenzen, die du kennen solltest:

- Die Sicherheit hängt am Passwort. Wer im Netz einen Host vortäuscht, könnte ein schwaches Passwort offline durchprobieren. Nimm deshalb ein langes Passwort.
- Der Host gehört nicht ins Internet. Gib den Port nicht im Router frei. Für Zugriff von unterwegs ist ein VPN der richtige Weg, zum Beispiel WireGuard oder Tailscale.
- Der Code wurde nicht von unabhängiger Seite geprüft.

## Grenzen

- Bildqualität: Standard sind 2560 px Breite, JPEG-Qualität 85 und volle Farbauflösung. Ein Bild hat damit etwa 350 KB. Über langsames WLAN hilft `--max-width 1600 --quality 70`.
- Es wird ein Monitor übertragen. Welcher, legt `--monitor N` fest.
- Beide Rechner sollten dasselbe Tastaturlayout haben, sonst kommen Sonderzeichen falsch an.
- Tastenkürzel des Systems wie Cmd+Tab oder die Windows-Taste bleiben auf dem eigenen Rechner.
- Wenn ein Mac einen Windows-Rechner steuert, den Client mit `--cmd-as-ctrl` starten. Dann wird Cmd+C zu Strg+C.

## Aus dem Quellcode starten

Voraussetzung ist Python 3.10 oder neuer.

```bash
pip install -r requirements.txt
python hopgrid.py                                # Auswahl Host/Client
python hopgrid.py --mode client --host Büro-PC   # direkt verbinden (Alias oder IP)
python host.py --cli                             # Host im Terminal
```

Optionen für den Host: `--mode host --alias Büro-PC --port 8080 --fps 15 --quality 85 --max-width 2560 --monitor 1`

## Selbst bauen

- **macOS:** `bash build_mac.sh` erzeugt `dist/HopGrid.app`.
- **Windows:** `build_windows.bat` erzeugt `dist\HopGrid.exe`.
- **GitHub Actions:** Ein Tag `v*` baut alle Varianten und legt sie als Release an.

**macOS signieren und notarisieren:** Dafür braucht es ein Apple-Developer-Konto mit einem Zertifikat vom Typ „Developer ID Application“. Sind die Secrets `MAC_CERT_P12_BASE64`, `MAC_CERT_PASSWORD`, `MAC_SIGN_IDENTITY`, `APPLE_ID`, `APPLE_TEAM_ID` und `APPLE_APP_PASSWORD` im Repo hinterlegt, signiert und notarisiert der Workflow die App automatisch. Dann entfällt die Gatekeeper-Sperre.

PyInstaller kann nicht für ein anderes System bauen. Die `.exe` entsteht deshalb auf Windows, die `.app` auf macOS.

## Aufbau

| Datei | Inhalt |
|---|---|
| `hopgrid.py` | Startfenster: Auswahl Host oder Client |
| `passwords.py` | Passwort im Schlüsselbund des Systems (keyring) |
| `dpi.py` | Windows: richtige Skalierung (sonst unscharf, Maus-Versatz) |
| `hostplatform.py` | Host: Zeigerform erkennen, Ordner unter dem Zeiger (Schreibtisch, Finder, Explorer) |
| `filetransfer.py` | Dateien/Ordner übertragen (Blöcke, sichere Pfade, kopierte Dateien in Finder/Explorer) |
| `ui.py` | Design: Farben (hell/dunkel), Schriften, Bausteine (customtkinter) |
| `autostart.py` | Autostart (Windows-Registry, macOS-LaunchAgent), Einstellungsdateien |
| `protocol.py` | Login, Schlüsselaustausch, verschlüsselter Kanal |
| `host.py` | Bildschirmaufnahme (mss und OpenCV als JPEG), Eingaben (pynput, auf macOS Quartz), Host-Fenster |
| `client.py` | Login-Fenster, Anzeige (Tkinter und Pillow), Weitergabe der Eingaben |

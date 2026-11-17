#!/usr/bin/env bash
# Baut dist/HopGrid.app (Host und Client in einer App) und dist/HopGrid-macOS.zip.
#
# Signatur:
#   - Mit MAC_SIGN_IDENTITY (Zertifikat im Schlüsselbund) wird damit signiert.
#     Eigenes Zertifikat "HopGrid Code Signing": macOS erkennt jede neue Version als
#     dieselbe App, Freigaben (Bedienungshilfen, Bildschirmaufnahme) bleiben erhalten.
#     Das Zertifikat muss dafür als vertrauenswürdig fürs Signieren eingetragen sein
#     (GitHub Actions erledigt das; lokal bräuchte es Administratorrechte).
#   - Zusätzlich APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD + Developer-ID-Zertifikat:
#     Hardened Runtime + Notarisierung (entfernt auch die Gatekeeper-Warnung).
#   - Sonst nur ad-hoc (Freigaben gehen bei jedem Update verloren).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt pyinstaller

.venv/bin/pyinstaller --noconfirm --clean --windowed --icon assets/icon.icns \
  --name HopGrid --osx-bundle-identifier com.kenomedia.hopgrid \
  --hidden-import host --hidden-import client --hidden-import keyring.backends.macOS \
  --hidden-import Foundation --hidden-import ApplicationServices --hidden-import pyperclip \
  --collect-data customtkinter --collect-all tkinterdnd2 --hidden-import AppKit \
  --add-data assets/icon.png:assets \
  --hidden-import pynput.keyboard._darwin --hidden-import pynput.mouse._darwin \
  --hidden-import pynput._util.darwin --hidden-import pynput._util.darwin_vks \
  hopgrid.py
rm -f HopGrid.spec

APP="dist/HopGrid.app"
ZIP="dist/HopGrid-macOS.zip"

# Erklärtexte für die macOS-Abfragen (Lokales Netzwerk usw.), danach neu signieren
PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :NSLocalNetworkUsageDescription string 'HopGrid verbindet sich direkt mit Rechnern in deinem lokalen Netz und sucht dort nach Freigaben.'" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSAppleEventsUsageDescription string 'HopGrid steuert diesen Rechner, wenn du ihn freigibst.'" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string ${HOPGRID_VERSION:-0.0.0}" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString ${HOPGRID_VERSION:-0.0.0}" "$PLIST"
if [ -z "${MAC_SIGN_IDENTITY:-}" ]; then
  echo "Hinweis: nur ad-hoc signiert – macOS-Freigaben gehen bei Updates verloren."
  codesign --force --deep --sign - "$APP"
elif [ -n "${APPLE_ID:-}" ]; then
  echo "Signiere (Developer ID, Hardened Runtime) mit: $MAC_SIGN_IDENTITY"
  codesign --force --deep --options runtime --timestamp \
    --entitlements assets/entitlements.plist --sign "$MAC_SIGN_IDENTITY" "$APP"
else
  echo "Signiere mit eigenem Zertifikat: $MAC_SIGN_IDENTITY"
  codesign --force --deep --sign "$MAC_SIGN_IDENTITY" "$APP"
fi
codesign --verify --deep --strict "$APP"
codesign -d -r- "$APP" 2>&1 | grep designated || true

rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"

if [ -n "${MAC_SIGN_IDENTITY:-}" ] && [ -n "${APPLE_ID:-}" ]; then
  echo "Notarisiere bei Apple …"
  xcrun notarytool submit "$ZIP" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" \
    --password "$APPLE_APP_PASSWORD" --wait
  xcrun stapler staple "$APP"
  rm -f "$ZIP"
  ditto -c -k --keepParent "$APP" "$ZIP"
fi

echo "Fertig: $APP und $ZIP"

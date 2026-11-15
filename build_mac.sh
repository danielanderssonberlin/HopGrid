#!/usr/bin/env bash
# Baut dist/HopGrid.app (Host und Client in einer App) und dist/HopGrid-macOS.zip.
#
# Optional signieren und notarisieren (beseitigt die Gatekeeper-Sperre), wenn gesetzt:
#   MAC_SIGN_IDENTITY   z. B. "Developer ID Application: Name (TEAMID)"
#   APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD   (App-spezifisches Passwort)
# Ohne diese Variablen wird nur ad-hoc signiert.
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
  --hidden-import pynput.keyboard._darwin --hidden-import pynput.mouse._darwin \
  --hidden-import pynput._util.darwin --hidden-import pynput._util.darwin_vks \
  hopgrid.py
rm -f HopGrid.spec

APP="dist/HopGrid.app"
ZIP="dist/HopGrid-macOS.zip"

if [ -n "${MAC_SIGN_IDENTITY:-}" ]; then
  echo "Signiere mit: $MAC_SIGN_IDENTITY"
  codesign --force --deep --options runtime --timestamp \
    --entitlements assets/entitlements.plist --sign "$MAC_SIGN_IDENTITY" "$APP"
  codesign --verify --strict --verbose=2 "$APP"
fi

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

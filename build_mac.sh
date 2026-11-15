#!/usr/bin/env bash
# Baut "HopGrid Host.app" und "HopGrid Client.app" nach dist/ und packt sie als ZIP.
# Die App läuft auf der Architektur, mit der gebaut wurde (Apple Silicon oder Intel).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt pyinstaller

PYI=(.venv/bin/pyinstaller --noconfirm --clean --windowed --icon assets/icon.icns)
MAC_HIDDEN=(--hidden-import pynput.keyboard._darwin --hidden-import pynput.mouse._darwin
            --hidden-import pynput._util.darwin --hidden-import pynput._util.darwin_vks)

"${PYI[@]}" "${MAC_HIDDEN[@]}" --name "HopGrid Host" \
  --osx-bundle-identifier com.kenomedia.hopgrid.host host.py
"${PYI[@]}" --name "HopGrid Client" \
  --osx-bundle-identifier com.kenomedia.hopgrid.client client.py

cd dist
rm -f HopGrid-Host-macOS.zip HopGrid-Client-macOS.zip
ditto -c -k --keepParent "HopGrid Host.app" HopGrid-Host-macOS.zip
ditto -c -k --keepParent "HopGrid Client.app" HopGrid-Client-macOS.zip
echo "Fertig: dist/HopGrid Host.app, dist/HopGrid Client.app (+ ZIPs)"

"""Erzeugt das App-Icon (assets/icon.png, icon.ico, icon.icns). Einmalig ausführen."""
import os
import subprocess
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")


def draw(size):
    scale = 4  # überabtasten für glatte Kanten
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = int(s * 0.06)
    d.rounded_rectangle([m, m, s - m, s - m], radius=int(s * 0.2), fill=(22, 52, 96, 255))
    # 3x3-Raster, ein Feld hervorgehoben ("Hop" zum Ziel-Rechner)
    cell, gap = s * 0.2, s * 0.05
    start = (s - (3 * cell + 2 * gap)) / 2
    for r in range(3):
        for c in range(3):
            x, y = start + c * (cell + gap), start + r * (cell + gap)
            color = (255, 176, 46, 255) if (r, c) == (0, 2) else (226, 236, 248, 255)
            d.rounded_rectangle([x, y, x + cell, y + cell], radius=int(cell * 0.22), fill=color)
    return img.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(ASSETS, exist_ok=True)
    big = draw(1024)
    big.save(os.path.join(ASSETS, "icon.png"))
    big.save(os.path.join(ASSETS, "icon.ico"), sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    if sys.platform == "darwin":
        iconset = os.path.join(ASSETS, "icon.iconset")
        os.makedirs(iconset, exist_ok=True)
        for px in (16, 32, 128, 256, 512):
            draw(px).save(os.path.join(iconset, f"icon_{px}x{px}.png"))
            draw(px * 2).save(os.path.join(iconset, f"icon_{px}x{px}@2x.png"))
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(ASSETS, "icon.icns")], check=True)
        for f in os.listdir(iconset):
            os.remove(os.path.join(iconset, f))
        os.rmdir(iconset)


if __name__ == "__main__":
    main()

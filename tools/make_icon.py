"""
Turns assets/icon-source.png into the app icons:
  assets/logo.ico      (exe + window icon, 16-256 px)
  assets/logo-64.png   (shown in the window header)

Crops the transparent border, pads to a square, then resizes.
Needs Pillow (installed automatically by build.bat).

    python tools/make_icon.py [source.png]
"""

import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "..", "assets")


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ASSETS, "icon-source.png")
    img = Image.open(src).convert("RGBA")
    bbox = img.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    if bbox:
        img = img.crop(bbox)
    side = max(img.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    os.makedirs(ASSETS, exist_ok=True)
    square.resize((64, 64), Image.LANCZOS).save(os.path.join(ASSETS, "logo-64.png"))
    square.resize((256, 256), Image.LANCZOS).save(
        os.path.join(ASSETS, "logo.ico"),
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("icons written to", os.path.abspath(ASSETS))


if __name__ == "__main__":
    main()

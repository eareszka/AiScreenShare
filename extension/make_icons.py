"""Generate the extension's PNG icons (16/48/128) into extension/icons/.

Run once after checkout (or whenever you change the look):
    py extension/make_icons.py

Chrome wants PNGs, not the .ico the desktop app used, so this is a separate
generator. The design is a simple cyan magnifier-on-dark mark, matching the
"watch a region" idea.
"""
import os
from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
BG = (17, 17, 17, 255)      # near-black, matches the side panel
FG = (0, 224, 255, 255)     # cyan accent (same as the desktop selection box)


def make(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size
    # rounded dark tile
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=max(2, s // 6), fill=BG)
    # magnifier ring
    pad = s * 0.22
    ring = [pad, pad, s * 0.68, s * 0.68]
    lw = max(1, s // 12)
    d.ellipse(ring, outline=FG, width=lw)
    # handle
    d.line([s * 0.62, s * 0.62, s * 0.82, s * 0.82], fill=FG, width=lw)
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    for size in (16, 48, 128):
        make(size).save(os.path.join(OUT, f"icon{size}.png"))
    print(f"wrote icons to {OUT}")


if __name__ == "__main__":
    main()

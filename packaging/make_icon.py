#!/usr/bin/env python3
"""Render the RSSVibes app icon (matches web/favicon.svg) to PNG + ICO.

Used by the release CI to give the Windows .exe and the Linux AppImage an icon.
Requires Pillow (installed in CI only): pip install pillow
"""
from PIL import Image, ImageDraw


def render(size):
    s = size / 32.0  # favicon.svg is authored on a 32x32 grid
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(7 * s), fill=(15, 155, 142, 255))

    def bar(x, y, w, h, alpha):
        d.rounded_rectangle([x * s, y * s, (x + w) * s, (y + h) * s], radius=int(2 * s),
                            fill=(255, 255, 255, alpha))

    bar(6, 6, 8, 20, 242)    # tall left bar
    bar(17, 6, 9, 9, 242)    # top-right square
    bar(17, 17, 9, 9, 178)   # bottom-right square (dimmer)
    return img


def main():
    master = render(256)
    master.save("rssvibes.png")
    master.save("rssvibes.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote rssvibes.png and rssvibes.ico")


if __name__ == "__main__":
    main()

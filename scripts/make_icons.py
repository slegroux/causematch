"""Draw the app icons into static/icons/: a rounded colored square with a white "C".

uv run python scripts/make_icons.py     (needs Pillow, a dev dependency; the PNGs are committed)
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "static" / "icons"
COLOR = "#2563eb"  # the manifest's theme_color


def icon(size, rounded=True, glyph=0.62):
    scale = 4  # draw large, then shrink: smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if rounded:
        draw.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 5, fill=COLOR)
    else:  # iOS rounds the corners itself
        draw.rectangle((0, 0, s, s), fill=COLOR)
    font = ImageFont.load_default(size=int(s * glyph))
    draw.text((s / 2, s / 2), "C", font=font, fill="white", anchor="mm")
    return img.resize((size, size), Image.LANCZOS)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    icon(192).save(OUT / "icon-192.png")
    icon(512).save(OUT / "icon-512.png")
    icon(512, rounded=False, glyph=0.5).convert("RGB").save(
        OUT / "icon-maskable-512.png"
    )
    icon(180, rounded=False).convert("RGB").save(OUT / "apple-touch-icon.png")
    icon(48).save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])


if __name__ == "__main__":
    main()

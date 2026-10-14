"""Builds frontend/public/og-image.png, the 1200x630 link-preview card.

The source logo is 6250x6250 (5.4 MB), far too heavy for a link preview:
WhatsApp and others drop images over a few hundred KB. This crops the logo to
its visible content and composes it on the console's own background colour,
using the console's font and accent (see src/styles.css).

    python frontend/scripts/make-og-image.py        # needs Pillow
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
LOGO = ROOT / "public" / "assets" / "risk-radar-logo.png"
FONT = ROOT / "public" / "assets" / "mona-sans-variable.ttf"
OUT = ROOT / "public" / "og-image.png"

W, H = 1200, 630
BG = (246, 247, 249)        # --bg
INK = (17, 24, 39)          # --text
INK_2 = (75, 85, 99)        # --text-2
ACCENT = (221, 79, 5)       # --accent


def font(size: int, weight: int) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FONT), size)
    try:
        axes = f.get_variation_axes()
        values = [weight if (a.get("name") in (b"Weight", "Weight")) else a["default"] for a in axes]
        f.set_variation_by_axes(values)
    except Exception:
        pass  # fall back to the font's default weight
    return f


def main() -> None:
    canvas = Image.new("RGB", (W, H), BG)

    logo = Image.open(LOGO).convert("RGBA")
    logo = logo.crop(logo.getchannel("A").getbbox())          # trim transparent margin
    target_h = 300
    logo = logo.resize((round(logo.width * target_h / logo.height), target_h), Image.LANCZOS)
    logo_x, logo_y = 90, (H - target_h) // 2
    canvas.paste(logo, (logo_x, logo_y), logo)

    d = ImageDraw.Draw(canvas)
    text_x = logo_x + logo.width + 60
    d.text((text_x, 190), "Risk Radar", font=font(104, 700), fill=INK)
    d.text((text_x, 322), "Real-time transaction fraud detection", font=font(36, 500), fill=INK_2)
    d.text((text_x, 380), "Explainable alerts. Investigable cases. Audited decisions.",
           font=font(25, 400), fill=INK_2)
    d.rectangle((0, H - 14, W, H), fill=ACCENT)

    canvas.save(OUT, optimize=True)
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB, {W}x{H})")


if __name__ == "__main__":
    main()

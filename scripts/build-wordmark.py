"""Render the website's "Clean My V[camera-i]deo" wordmark to PNG.

The site renders the wordmark with HTML + a custom SVG camera icon as the
dot of the "i". We can't ship that directly into a Tk window, so we
pre-bake transparent PNGs with Pillow and load them at runtime.

The output goes into ``assets/wordmark/`` and is committed to the repo.
The script is build-time only: end users never run it.

Run:
    python scripts/build-wordmark.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "assets" / "wordmark"

# Same hex values as site/style.css :root.
ACCENT_HI = (0x81, 0x8c, 0xf8)      # --accent-hi
ACCENT_GLOW = (0xa7, 0x8b, 0xfa)    # --accent-glow
COOL = (0x22, 0xd3, 0xee)           # --cool
CAM_COLOR = (0xff, 0x5f, 0x7a)      # camera silhouette
WHITE = (0xff, 0xff, 0xff)

# Two text segments. The "V"+"deo" portion uses the gradient; the
# vertical stem of the "i" sits between them in the same gradient,
# topped by the camera.
LEFT_TEXT = "Clean My V"
RIGHT_TEXT = "deo."


# Inter Bold paths to try in order. The script only runs on dev
# machines, so we only need ONE of these to resolve.
INTER_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/inter-zorin-os/Inter-Bold.ttf",
    "/usr/share/fonts/truetype/inter/Inter-Bold.ttf",
    "/usr/share/fonts/Inter-Bold.ttf",
    str(Path.home() / ".local" / "share" / "fonts" / "Inter-Bold.ttf"),
]
DEJAVU_BOLD_FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _resolve_font(size: int) -> ImageFont.FreeTypeFont:
    for c in INTER_BOLD_CANDIDATES:
        if Path(c).is_file():
            return ImageFont.truetype(c, size=size)
    return ImageFont.truetype(DEJAVU_BOLD_FALLBACK, size=size)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


def _three_stop_color(t: float) -> tuple[int, int, int]:
    """linear-gradient(accent-hi 10%, accent-glow 50%, cool 95%) sampled at t in [0..1]."""
    if t <= 0.10:
        return ACCENT_HI
    if t >= 0.95:
        return COOL
    if t <= 0.50:
        local = (t - 0.10) / (0.50 - 0.10)
        return _lerp(ACCENT_HI, ACCENT_GLOW, local)
    local = (t - 0.50) / (0.95 - 0.50)
    return _lerp(ACCENT_GLOW, COOL, local)


def _gradient_strip(width: int, height: int) -> Image.Image:
    """Build a horizontal three-stop gradient image."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    px = img.load()
    for x in range(width):
        t = x / max(1, width - 1)
        r, g, b = _three_stop_color(t)
        for y in range(height):
            px[x, y] = (r, g, b, 255)
    return img


def _text_to_alpha(text: str, font: ImageFont.FreeTypeFont) -> Image.Image:
    """Render `text` to an L (alpha) image just large enough to hold it."""
    # Measure with a temporary canvas first.
    tmp = Image.new("L", (1, 1), 0)
    bbox = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font, anchor="lt")
    w = max(1, bbox[2] - bbox[0])
    h = max(1, bbox[3] - bbox[1])
    img = Image.new("L", (w + 4, h + 4), 0)
    ImageDraw.Draw(img).text((-bbox[0] + 2, -bbox[1] + 2), text, font=font, fill=255)
    return img


def _camera_silhouette(target_h: int) -> Image.Image:
    """Hand-drawn approximation of the site SVG camera glyph.

    Site SVG viewBox is 24x18 (path: rounded-rect body + small viewfinder
    flap on top + center lens circle). We rasterise a faithful version
    at any pixel size by drawing in floats and then resizing once.
    """
    # Internal "logical" canvas at 240 x 180 (10x viewBox); supersample.
    LW, LH = 240, 180
    img = Image.new("RGBA", (LW, LH), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Body: 240 wide, 130 tall, sitting at y=30..160. Rounded ~20px.
    body_box = (0, 30, LW, 160)
    d.rounded_rectangle(body_box, radius=20, fill=CAM_COLOR)
    # Top viewfinder flap: 90 x 30, centred horizontally, sitting on top
    # of the body (subtle bump to break the rectangle silhouette).
    flap_w = 90
    flap_x0 = (LW - flap_w) // 2
    d.rounded_rectangle(
        (flap_x0, 0, flap_x0 + flap_w, 36), radius=8, fill=CAM_COLOR
    )
    # Lens cutout: pure transparent circle. Use mask-paste so the hole
    # actually punches through.
    cx, cy, cr = LW // 2, 95, 38
    hole = Image.new("L", (LW, LH), 255)
    ImageDraw.Draw(hole).ellipse((cx - cr, cy - cr, cx + cr, cy + cr), fill=0)
    # Re-apply alpha through the hole.
    a = img.split()[3]
    a = Image.eval(a, lambda v: v)
    a.paste(hole, (0, 0), hole)  # type: ignore[arg-type]
    img.putalpha(a)
    # Resize to target.
    target_w = int(target_h * (LW / LH))
    return img.resize((target_w, target_h), Image.LANCZOS)


def _gradient_text(text: str, font: ImageFont.FreeTypeFont) -> Image.Image:
    """Render `text` filled with the wordmark gradient. Returns RGBA."""
    alpha = _text_to_alpha(text, font)
    grad = _gradient_strip(alpha.width, alpha.height)
    out = Image.new("RGBA", alpha.size, (0, 0, 0, 0))
    out.paste(grad, (0, 0), alpha)
    return out


def _white_text(text: str, font: ImageFont.FreeTypeFont) -> Image.Image:
    alpha = _text_to_alpha(text, font)
    out = Image.new("RGBA", alpha.size, (0, 0, 0, 0))
    rgba = Image.new("RGBA", alpha.size, WHITE + (255,))
    out.paste(rgba, (0, 0), alpha)
    return out


def render_wordmark(width: int) -> Image.Image:
    """Render the wordmark at approximately `width` pixels wide.

    Layout (left to right):
      "Clean My V"   - white "Clean My ", gradient "V"
      [stem]         - thin gradient bar (the "i" stem)
      [camera]       - red-pink camera centred above the stem
      "deo."         - gradient

    The stem and camera are stacked vertically so the camera sits where
    the "i" dot would. The camera's bottom and the stem's top almost
    touch, with a tiny gap to mimic the dotted "i".
    """
    # Pick a font size such that "Clean My Video" (no decorations) fits
    # roughly within `width`. Iterate up until we exceed.
    font_size = 16
    last = font_size
    while True:
        f = _resolve_font(font_size)
        approx = _text_to_alpha("Clean My Video.", f).width
        if approx > width * 0.86:
            break
        last = font_size
        font_size += 2
    font = _resolve_font(last)

    # Render the four pieces.
    left_white = _white_text("Clean My ", font)
    v_glyph = _gradient_text("V", font)
    right_glyph = _gradient_text("deo.", font)

    text_h = v_glyph.height
    # Stem dimensions: ~6% of cap height wide, ~58% tall
    stem_w = max(2, int(text_h * 0.07))
    stem_h = max(8, int(text_h * 0.58))
    # Camera height ~40% of cap height
    cam_h = max(10, int(text_h * 0.42))
    camera = _camera_silhouette(cam_h)

    # i-stem fill = average gradient color at the stem's eventual x.
    stem = Image.new("RGBA", (stem_w, stem_h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stem)
    for y in range(stem_h):
        sd.line([(0, y), (stem_w, y)], fill=ACCENT_GLOW + (255,))

    gap = max(2, int(text_h * 0.02))   # gap between camera and stem
    cluster_w = max(stem_w, camera.width)
    cluster_h = cam_h + gap + stem_h

    # Total composition width. Vertical padding has to be generous
    # enough that the camera silhouette never kisses the top edge of
    # the bitmap. Some Tk themes shave a few pixels of the top of any
    # widget hosting an image, so we err on the side of way too much
    # top air (45 % of cap height) - the bitmap fits in the same
    # header height because the header just centres it vertically.
    pad_x = max(8, int(text_h * 0.10))
    pad_y_top = max(22, int(text_h * 0.45))
    pad_y_bottom = max(8, int(text_h * 0.12))
    total_w = (
        left_white.width
        + v_glyph.width
        + cluster_w
        + right_glyph.width
        + pad_x * 2
    )
    total_h = max(text_h, cluster_h) + pad_y_top + pad_y_bottom
    canvas = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))

    # Anchor the text vertically near the bottom of the available area
    # and the camera+stem cluster at the top, so the camera always has
    # `pad_y_top` clean pixels above it.
    baseline_y = total_h - text_h - pad_y_bottom
    cluster_y0 = pad_y_top

    x = pad_x
    canvas.paste(left_white, (x, baseline_y), left_white)
    x += left_white.width
    canvas.paste(v_glyph, (x, baseline_y), v_glyph)
    x += v_glyph.width
    # Camera centred horizontally inside cluster column.
    cam_x = x + (cluster_w - camera.width) // 2
    canvas.paste(camera, (cam_x, cluster_y0), camera)
    # Stem under the camera.
    stem_x = x + (cluster_w - stem_w) // 2
    canvas.paste(stem, (stem_x, cluster_y0 + cam_h + gap), stem)
    x += cluster_w
    canvas.paste(right_glyph, (x, baseline_y), right_glyph)

    return canvas


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    targets = [256, 384, 512, 768]
    for w in targets:
        img = render_wordmark(w)
        out = OUT_DIR / f"wordmark-{w}.png"
        img.save(out, "PNG", optimize=True)
        print(f"  wrote {out.relative_to(REPO_ROOT)}  ({img.width}x{img.height})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

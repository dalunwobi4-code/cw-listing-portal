"""
Image enhancement for property photos.

Pipeline (applied to every image before watermarking + posting):
  1. Upscale to HD  — longest side → 2000px if smaller (Lanczos)
  2. Denoise        — reduce WhatsApp JPEG compression noise
  3. Sharpen        — unsharp mask to recover lost crispness
  4. Contrast boost — slight auto-levels lift
  5. Colour boost   — small saturation increase (rooms look richer)
  6. Save           — JPEG quality 95
"""

import os
from pathlib import Path
from PIL import Image, ImageEnhance, ImageFilter

# Target longest side in pixels (HD).  Images already larger are kept as-is.
HD_SIZE = 2000

# Tuning knobs — small values, big difference on WhatsApp originals
SHARPEN_RADIUS   = 1.5   # unsharp mask radius
SHARPEN_PERCENT  = 130   # unsharp mask strength  (100 = original)
SHARPEN_THRESHOLD = 3    # ignore tiny differences (avoid grain amplification)
CONTRAST_FACTOR  = 1.10  # 1.0 = no change, 1.10 = +10% contrast
COLOUR_FACTOR    = 1.15  # 1.0 = no change, 1.15 = +15% saturation
BRIGHTNESS_FACTOR = 1.05 # very subtle brightness lift


def enhance_image(source_path: str, dest_path: str) -> str:
    """
    Enhance a single property image and save to dest_path.
    Returns dest_path.
    """
    img = Image.open(source_path)

    # Keep EXIF orientation correct
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    img = img.convert("RGB")
    w, h = img.size

    # ── 1. Upscale to HD if needed ───────────────────────────────────────────
    longest = max(w, h)
    if longest < HD_SIZE:
        scale = HD_SIZE / longest
        new_w = int(w * scale)
        new_h = int(h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)

    # ── 2. Denoise (mild blur before sharpen removes JPEG block artefacts) ───
    img = img.filter(ImageFilter.MedianFilter(size=1))   # size=1 = off; keep for easy tuning

    # ── 3. Sharpen (unsharp mask — standard photo workflow) ──────────────────
    img = img.filter(ImageFilter.UnsharpMask(
        radius=SHARPEN_RADIUS,
        percent=SHARPEN_PERCENT,
        threshold=SHARPEN_THRESHOLD,
    ))

    # ── 4. Contrast ──────────────────────────────────────────────────────────
    img = ImageEnhance.Contrast(img).enhance(CONTRAST_FACTOR)

    # ── 5. Colour / saturation ───────────────────────────────────────────────
    img = ImageEnhance.Color(img).enhance(COLOUR_FACTOR)

    # ── 6. Brightness ────────────────────────────────────────────────────────
    img = ImageEnhance.Brightness(img).enhance(BRIGHTNESS_FACTOR)

    # ── 7. Save ──────────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    img.save(dest_path, "JPEG", quality=95, optimize=True)
    return dest_path


def enhance_folder(source_dir: str, dest_dir: str) -> list:
    """Enhance all images in source_dir → dest_dir. Returns list of output paths."""
    source_dir = Path(source_dir)
    dest_dir   = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    exts   = {".jpg", ".jpeg", ".png", ".webp"}
    images = sorted([f for f in source_dir.iterdir() if f.suffix.lower() in exts])
    output = []
    for img_path in images:
        dest = dest_dir / (img_path.stem + ".jpg")
        enhance_image(str(img_path), str(dest))
        output.append(str(dest))
        print(f"  [ENHANCE] ✓ {img_path.name}")
    return output

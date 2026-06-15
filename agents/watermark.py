"""
Image watermarking agent — applies CW logo to all property images
Run standalone: python3 watermark.py <source_dir> <output_dir>
"""

import os, sys
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass


def apply_watermark(source_path: str, dest_path: str, logo_path: str = None, opacity: float = 1.0):
    """Apply CW watermark to a single image.

    Renders the logo on a semi-transparent dark pill so the white logo
    is always visible regardless of the photo background (bright walls, sky, etc.).
    """
    img = Image.open(source_path).convert("RGBA")
    w, h = img.size

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))

    if logo_path and os.path.exists(logo_path):
        logo = Image.open(logo_path).convert("RGBA")

        # Scale logo to ~35% of image width
        target_w = max(200, int(w * 0.35))
        scale    = target_w / logo.width
        new_w    = target_w
        new_h    = max(1, int(logo.height * scale))
        logo     = logo.resize((new_w, new_h), Image.LANCZOS)

        # Apply opacity to alpha channel
        r, g, b, a = logo.split()
        a    = a.point(lambda x: int(x * opacity))
        logo = Image.merge("RGBA", (r, g, b, a))

        # Centre of image
        px = (w - new_w) // 2
        py = (h - new_h) // 2
        overlay.paste(logo, (px, py), logo)

    else:
        # Fallback: centred text watermark
        draw_tmp  = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        font_size = max(20, w // 20)
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except Exception:
            font = ImageFont.load_default()
        text = "CW Lagos"
        bbox = draw_tmp.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw = ImageDraw.Draw(overlay)
        draw.text(((w - tw) // 2, (h - th) // 2), text,
                  fill=(255, 255, 255, int(255 * opacity)), font=font)

    watermarked = Image.alpha_composite(img, overlay)
    watermarked = watermarked.convert("RGB")
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    watermarked.save(dest_path, "JPEG", quality=92)
    return dest_path


def watermark_property_folder(source_dir: str, dest_dir: str, logo_path: str = None):
    """Watermark all images in a folder. Returns list of output paths."""
    source_dir = Path(source_dir)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    output_paths = []
    images = sorted([f for f in source_dir.iterdir()
                     if f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif')])

    for i, img_path in enumerate(images):
        dest_path = dest_dir / f"img{i+1:02d}.jpg"
        apply_watermark(str(img_path), str(dest_path), logo_path)
        output_paths.append(str(dest_path))
        print(f"  ✓ {img_path.name} → {dest_path.name}")

    return output_paths


def watermark_batch(properties: list, base_source_dir: str, base_dest_dir: str, logo_path: str = None):
    """
    Watermark images for multiple properties in parallel.
    properties: list of dicts with 'ref' key (e.g. 'CW08494')
    """
    import concurrent.futures

    results = {}

    def process_one(prop):
        ref = prop['ref']
        src = os.path.join(base_source_dir, ref)
        dst = os.path.join(base_dest_dir, ref)
        if not os.path.exists(src):
            return ref, []
        paths = watermark_property_folder(src, dst, logo_path)
        return ref, paths

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(process_one, p): p['ref'] for p in properties}
        for future in concurrent.futures.as_completed(futures):
            ref, paths = future.result()
            results[ref] = paths
            print(f"[WATERMARK] {ref}: {len(paths)} images done")

    return results


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 watermark.py <source_dir> <output_dir> [logo_path]")
        sys.exit(1)
    src, dst = sys.argv[1], sys.argv[2]
    logo = sys.argv[3] if len(sys.argv) > 3 else None
    paths = watermark_property_folder(src, dst, logo)
    print(f"\nDone: {len(paths)} images watermarked → {dst}")

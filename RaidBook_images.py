# -*- coding: utf-8 -*-
import io
import os

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover - runtime fallback
    Image = None
    ImageOps = None


DEFAULT_CANVAS_SIZE = (960, 540)


def parse_canvas_size(value, default=DEFAULT_CANVAS_SIZE):
    """Parse image canvas size from config. Accepts [w, h], "960x540", or {"width": 960, "height": 540}."""
    try:
        if isinstance(value, dict):
            width = int(value.get("width") or value.get("w"))
            height = int(value.get("height") or value.get("h"))
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            width = int(value[0])
            height = int(value[1])
        elif isinstance(value, str) and "x" in value.lower():
            left, right = value.lower().split("x", 1)
            width = int(left.strip())
            height = int(right.strip())
        else:
            return default
        if width <= 0 or height <= 0:
            return default
        return width, height
    except Exception:
        return default


def normalized_image_buffer(path, *, canvas_size=DEFAULT_CANVAS_SIZE):
    """Return a PNG BytesIO normalized to a fixed transparent canvas, or None if Pillow cannot read it."""
    if Image is None or ImageOps is None:
        return None

    with Image.open(path) as src:
        image = src.convert("RGBA")

    alpha_bbox = image.getchannel("A").getbbox()
    if alpha_bbox:
        image = image.crop(alpha_bbox)

    width, height = canvas_size
    image = ImageOps.contain(image, (width, height), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    x = (width - image.width) // 2
    y = (height - image.height) // 2
    canvas.alpha_composite(image, (x, y))

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer


def normalized_image_file_args(path, *, filename_prefix="image", canvas_size=DEFAULT_CANVAS_SIZE, logger=None):
    """Return (file_obj, filename) for discord.File. Falls back to the original file if normalization fails."""
    try:
        buffer = normalized_image_buffer(path, canvas_size=canvas_size)
        if buffer is not None:
            return buffer, f"{filename_prefix}.png"
    except Exception as e:
        if logger:
            logger.info(f"[이미지 정규화 실패] {path}: {e}")

    try:
        ext = os.path.splitext(path)[1].lower() or ".png"
        return path, f"{filename_prefix}{ext}"
    except Exception:
        return None, ""

"""
src/utils/image_utils.py
Helpers for combining statement screenshots before sending them to Gemini.
"""
from __future__ import annotations

import io

from PIL import Image


def stitch_images_vertically(images: list[bytes]) -> bytes:
    """
    Stack screenshots top-to-bottom (in the given order) into one PNG, scaling each to a
    common width so a multi-screenshot statement reads like one long page.
    """
    decoded = [Image.open(io.BytesIO(b)).convert("RGB") for b in images]
    width = max(img.width for img in decoded)
    scaled = [
        img if img.width == width else img.resize((width, round(img.height * width / img.width)))
        for img in decoded
    ]

    canvas = Image.new("RGB", (width, sum(img.height for img in scaled)), "white")
    top = 0
    for img in scaled:
        canvas.paste(img, (0, top))
        top += img.height

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()

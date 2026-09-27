"""Pixels: the only module in ``cua`` that decodes an image.

Everything above it deals in PNG bytes and ``Rect`` boxes. Decoding needs
Pillow and numpy, which are the ``vision`` extra; a build without them has no
vision fallback (``available``), and says so rather than failing half way
through a run.

Boxes are in viewport pixels, the coordinate space every ``Node.bbox`` uses.
A screenshot taken at a device pixel ratio other than 1 is larger than the
viewport; ``scale_of`` measures that, and the detector refuses a picture whose
scale differs from the one a control was recorded at rather than matching a
template against pixels of another size.
"""

from __future__ import annotations

import hashlib
import io
import math
from typing import Any, TypeAlias

from cua.surface.protocol import Rect, Viewport

Pixels: TypeAlias = Any
"""A 2-D numpy array of floats. Typed loosely: numpy is an optional extra,
and its stubs are not read by the type checker (see ``pyproject.toml``)."""


class VisionUnavailable(RuntimeError):
    """The ``vision`` extra (Pillow, numpy) is not installed."""


def available() -> bool:
    try:
        import numpy  # noqa: F401
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def _require() -> None:
    if not available():
        raise VisionUnavailable(
            "the vision fallback needs Pillow and numpy: pip install 'inter-cua[vision]'"
        )


def size_of(png: bytes) -> tuple[int, int]:
    """Width and height in pixels."""
    _require()
    from PIL import Image

    with Image.open(io.BytesIO(png)) as img:
        return img.size


def scale_of(png: bytes, viewport: Viewport) -> float:
    """Screenshot pixels per viewport pixel (the device pixel ratio it was
    taken at)."""
    w, _ = size_of(png)
    return w / viewport.w if viewport.w else 1.0


def pixel_box(rect: Rect, *, bounds: tuple[int, int]) -> tuple[int, int, int, int]:
    """A box in whole pixels that covers ``rect``, clipped to an image of
    size ``bounds``: (left, top, right, bottom)."""
    width, height = bounds
    left = max(0, math.floor(rect.x))
    top = max(0, math.floor(rect.y))
    right = min(width, math.ceil(rect.right))
    bottom = min(height, math.ceil(rect.bottom))
    return left, top, max(left, right), max(top, bottom)


def crop(png: bytes, rect: Rect) -> bytes:
    """The pixels of ``rect`` as a PNG of their own."""
    _require()
    from PIL import Image

    with Image.open(io.BytesIO(png)) as img:
        box = pixel_box(rect, bounds=img.size)
        region = img.convert("RGB").crop(box)
    out = io.BytesIO()
    region.save(out, format="PNG", optimize=True)
    return out.getvalue()


def pixels_sha256(png: bytes, rect: Rect | None = None) -> str:
    """SHA-256 of the RGB pixels of ``rect`` (of the whole image when None).

    Of the pixels, not of the file: two encoders write the same picture as
    different bytes, and what a guard has to prove is that the screen shows
    the same picture.
    """
    _require()
    from PIL import Image

    with Image.open(io.BytesIO(png)) as img:
        rgb = img.convert("RGB")
        if rect is not None:
            rgb = rgb.crop(pixel_box(rect, bounds=rgb.size))
        return hashlib.sha256(rgb.tobytes()).hexdigest()


def gray(png: bytes) -> Pixels:
    """Luminance as floats in [0, 1]."""
    _require()
    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(png)) as img:
        return np.asarray(img.convert("L"), dtype=np.float64) / 255.0

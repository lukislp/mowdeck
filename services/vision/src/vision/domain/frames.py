"""Camera frames: is one too dark for the vision model, do two form a stereo pair?

Pure computation on the image bytes (Pillow, through ``mowdeck-control[photo]``) and on the
frames' metadata; no I/O.
"""

import io
from collections.abc import Sequence
from typing import Final

from vision.domain.models import Image

MAX_LUMINANCE: Final = 255.0


def mean_luminance(data: bytes) -> float | None:
    """The mean luminance (ITU-R 601 luma, 0 black to 255 white) of a JPEG or PNG image; None
    when the image cannot be decoded (or Pillow is missing)."""
    try:
        from PIL import Image, ImageStat, UnidentifiedImageError
    except ImportError:  # pragma: no cover - Pillow comes with mowdeck-control[photo]
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.draft("L", (160, 120))  # JPEG: decode a downscaled greyscale, enough for a mean
            grey = image.convert("L")
            return float(ImageStat.Stat(grey).mean[0])
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def too_dark(data: bytes, threshold: float) -> bool:
    """True when ``threshold`` is positive and the image's mean luminance lies below it.
    An image that cannot be decoded is never too dark (the analysis just goes on)."""
    if threshold <= 0:
        return False
    luminance = mean_luminance(data)
    return luminance is not None and luminance < threshold


STEREO_MAX_SKEW_MS: Final = 500
"""Default for how far apart the two frames of a stereo pair may be captured (the browser aims
at 200 ms; the service tolerates more, a moving mower is still in practice)."""


def stereo_pair(images: Sequence[Image], max_skew_ms: float = STEREO_MAX_SKEW_MS) -> list[Image]:
    """The two images as ``[left, right]`` when they form a stereo pair, else an empty list.

    A pair is exactly two uploaded frames, one ``left`` and one ``right``, from two different
    cameras, captured at most ``max_skew_ms`` apart (when both carry a capture time; without
    times the pair is accepted as the browser sent it).
    """
    if len(images) != 2:
        return []
    by_side = {image.side: image for image in images}
    left, right = by_side.get("left"), by_side.get("right")
    if left is None or right is None or left.source != "upload" or right.source != "upload":
        return []
    if left.camera is not None and left.camera == right.camera:
        return []
    skew = capture_skew_ms(left, right)
    if skew is not None and skew > max_skew_ms:
        return []
    return [left, right]


def capture_skew_ms(first: Image, second: Image) -> float | None:
    """Milliseconds between the capture times of two frames; None when one has no time."""
    if first.captured_at is None or second.captured_at is None:
        return None
    return abs((first.captured_at - second.captured_at).total_seconds()) * 1000

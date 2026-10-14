"""Corresponding points for the camera panorama calibration (ADR 0037): plain data, no I/O.

The web app stitches the two front cameras with a homography it estimates itself (RANSAC). With
the AI-assisted method the vision model only supplies the raw material: 8 to 16 points that show
the same thing in the left and the right frame, in pixel coordinates of each image. Nothing is
proposed, approved or driven; the answer goes straight back to the browser.
"""

import io
from dataclasses import dataclass
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vision.domain.models import Usage

MIN_POINTS: Final = 8
"""What the prompt asks for at least."""
MAX_POINTS: Final = 16
"""What the prompt asks for at most (more are cut)."""
MIN_USABLE_POINTS: Final = 4
"""A homography needs four correspondences; fewer valid ones fail the call."""
LABEL_MAX: Final = 60

Coordinate = Annotated[list[float], Field(min_length=2, max_length=2)]


class PointPair(BaseModel):
    """One correspondence: the same scene point in the left and in the right frame."""

    model_config = ConfigDict(extra="forbid")

    left: Coordinate
    right: Coordinate
    label: str = ""

    @field_validator("label", mode="before")
    @classmethod
    def _cap_label(cls, value: Any) -> Any:
        return value[:LABEL_MAX] if isinstance(value, str) else value


class CorrespondencesOut(BaseModel):
    """The model's answer, validated before it leaves the service."""

    model_config = ConfigDict(extra="forbid")

    points: list[PointPair] = Field(default_factory=list)

    @field_validator("points", mode="before")
    @classmethod
    def _cap_points(cls, value: Any) -> Any:
        return value[:MAX_POINTS] if isinstance(value, list) else value


@dataclass(frozen=True, slots=True)
class FrameSize:
    """The pixel size of an image."""

    width: int
    height: int

    def contains(self, point: list[float], tolerance: float = 0.5) -> bool:
        """The point lies on the image (a half pixel of slack on each edge)."""
        x, y = point
        return (
            -tolerance <= x <= self.width + tolerance and -tolerance <= y <= self.height + tolerance
        )


@dataclass(frozen=True, slots=True)
class Correspondences:
    """The provider port's answer: the points plus who gave them and what it cost."""

    result: CorrespondencesOut
    model: str
    family: str
    usage: Usage


def usable_points(points: list[PointPair], left: FrameSize, right: FrameSize) -> list[PointPair]:
    """The points that lie on both images (a model may invent coordinates outside them),
    without exact duplicates."""
    seen: set[tuple[float, float, float, float]] = set()
    kept: list[PointPair] = []
    for point in points:
        key = (point.left[0], point.left[1], point.right[0], point.right[1])
        if key in seen or not (left.contains(point.left) and right.contains(point.right)):
            continue
        seen.add(key)
        kept.append(point)
    return kept


def image_size(data: bytes) -> FrameSize | None:
    """The pixel size of a JPEG or PNG; None when it cannot be decoded (or Pillow is missing)."""
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:  # pragma: no cover - Pillow comes with mowdeck-control[photo]
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    return FrameSize(int(width), int(height)) if width > 0 and height > 0 else None

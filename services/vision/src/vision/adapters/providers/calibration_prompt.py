"""The prompt and answer schema for corresponding points (panorama calibration, ADR 0037).

The model gets the left and the right front camera frame of the same moment and returns 8 to 16
points that show the same scene detail in both, in pixel coordinates of each image. It does not
compute any geometry: the browser fits the homography with RANSAC, which also throws out the
points the model got wrong.
"""

import base64
import json
from typing import Any, Final

from vision.domain.calibration import LABEL_MAX, MAX_POINTS, MIN_POINTS, FrameSize
from vision.domain.models import Image

CALIBRATION_PROMPT: Final = f"""\
You help calibrate the two front cameras of a robotic lawn mower so their pictures can be \
stitched into one panorama. You get two photos taken at the same moment: image 1 from the left \
camera, image 2 from the right camera. Their fields of view overlap.

Find between {MIN_POINTS} and {MAX_POINTS} distinct scene points that are clearly visible in \
BOTH images, and give each point's pixel position in each image. Pixel coordinates start at the \
top left corner (x to the right, y down) and refer to the image sizes stated in the message.

Rules:
- Pick sharp, unambiguous details: corners of objects, ends of edges, small high-contrast \
marks, the corner of a paving stone. Avoid uniform grass, sky, reflections and anything that \
moves (leaves in the wind, people, animals).
- Spread the points over the whole overlapping region, near and far, top and bottom; do not \
cluster them.
- Only report a point when you are sure it is the same detail in both images. Fewer good points \
are better than many guessed ones.
- `label`: two to five words naming the detail (at most {LABEL_MAX} characters).
Answer with one JSON object only."""

CORRESPONDENCE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["points"],
    "properties": {
        "points": {
            "type": "array",
            "description": f"{MIN_POINTS} to {MAX_POINTS} corresponding points.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["left", "right", "label"],
                "properties": {
                    "left": {
                        "type": "array",
                        "description": "[x, y] in pixels of image 1 (left camera).",
                        "items": {"type": "number"},
                    },
                    "right": {
                        "type": "array",
                        "description": "[x, y] in pixels of image 2 (right camera).",
                        "items": {"type": "number"},
                    },
                    "label": {"type": "string"},
                },
            },
        }
    },
}
"""The answer schema in the subset every structured output implementation accepts (the pair
length and the bounds are enforced by the Pydantic validation and the service)."""

CORRESPONDENCE_FORMAT: Final[dict[str, Any]] = {
    "type": "json_schema",
    "json_schema": {"name": "correspondences", "schema": CORRESPONDENCE_SCHEMA, "strict": True},
}

CORRESPONDENCE_PROMPT_SUFFIX: Final = (
    "\n\nReturn exactly one JSON object (no Markdown, no prose) that matches this JSON schema:\n"
    + json.dumps(CORRESPONDENCE_SCHEMA, separators=(",", ":"))
)


def calibration_system_prompt(*, prompt_mode: bool) -> str:
    """The system prompt; in prompt mode with the schema appended."""
    return CALIBRATION_PROMPT + (CORRESPONDENCE_PROMPT_SUFFIX if prompt_mode else "")


def calibration_content(
    left: Image, right: Image, left_size: FrameSize, right_size: FrameSize
) -> list[dict[str, Any]]:
    """The user message: the image sizes, then the left and the right frame as data URLs."""
    text = (
        f"Image 1 (left camera) is {left_size.width} x {left_size.height} pixels. "
        f"Image 2 (right camera) is {right_size.width} x {right_size.height} pixels."
    )
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for label, image in (("Image 1: left camera", left), ("Image 2: right camera", right)):
        parts.append({"type": "text", "text": label})
        encoded = base64.b64encode(image.data).decode("ascii")
        parts.append(
            {"type": "image_url", "image_url": {"url": f"data:{image.media_type};base64,{encoded}"}}
        )
    return parts

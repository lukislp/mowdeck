"""The system prompt, the answer schema and the message layout shared by every model family.

Structured output: models that support a JSON schema get it as ``response_format`` (LiteLLM maps
it to the family's mechanism, for Claude the native structured outputs); the others get the
schema in the prompt and their text is parsed with :func:`extract_json`. Either way the answer
is validated with :class:`~vision.domain.models.AssessmentOut` before anything is shown.
"""

import base64
import json
import re
from typing import Any, Final

from mowdeck_contracts.v1 import VisionTask

from vision.domain.context import SceneContext
from vision.domain.mapimage import CAPTION as MAP_CAPTION
from vision.domain.models import (
    MAX_STEP_SECONDS,
    MAX_STEPS,
    Image,
    ProposalAction,
    RiskFlag,
    Situation,
)

SYSTEM_PROMPT: Final = f"""\
You are the vision assistant of MowDeck, a control system for robotic lawn mowers. A mower is \
stuck or away from its charging station. You get images (from the mower's camera, from the \
user's phone, and, when `map_image` is true, as the last image a top-down rendering of the map \
around the mower) and a JSON scene context. Decide what is going on and choose the safest \
`action`. A human reviews every proposal before anything runs.

Actions:
- `return_to_dock`: the normal dock command. The mower drives home by itself on its mapped areas \
and transfer paths with its own sensors. This is the lowest-risk option: prefer it whenever \
`dock_route.return_to_dock_applicable` is true, also for task `recover` (a trapped mower in a \
mapped area usually finds its way home), unless the images clearly show that the mower is wedged \
and must first be freed by a short drive. Leave `proposal` empty.
- `drive`: a short maneuver in `proposal`.
- `none`: nothing is safe; explain in `abort_reasons`.

The mower: a robotic lawn mower about 0.7 m long and 0.6 m wide that drives like a tank. \
`linear` goes from -1 (full speed backward) to 1 (full speed forward), `angular` from -1 (turn \
right) to 1 (turn left); both can be combined. `motion` in the context says how fast that is. \
The blades are always off during your maneuver and you cannot turn them on.
Sensors: bumper and lift/tilt sensors stop the mower by themselves; RTK positioning. \
`mower.sensor_stop` true (lifted, tilted, a safety stop) means nothing may move the mower: \
action `none`. `drive_recoverable` is only reported with activity `error`; a paused or idle \
mower without an error may be driven and may return to the dock.

Geometry in the context is in the mower frame: `forward` metres ahead, `left` metres to the left \
of the mower. `shapes` are the mapped areas, transfer paths, no-go zones and obstacles nearby; \
`dock` is the charging station with its distance and bearing (left positive). `dock_route` says \
which area the mower and the dock are in, whether mapped paths link them and whether return to \
dock is applicable (with the reason). `recent_track` are the last positions with an RTK fix. The \
map image shows the same, heading up (the top edge is ahead of the mower).

Rules:
- At most {MAX_STEPS} steps, each at most {MAX_STEP_SECONDS:g} seconds. Prefer small, slow moves \
(|linear| <= 0.6). Back away from an obstacle before turning.
- Stay inside the mapped areas; never drive into a no-go zone or an obstacle.
- Task `recover`: free the mower so it can continue. Task `find_dock`: bring the mower home; \
with `drive` only bring it closer to the dock and roughly facing it (the system then sends a \
normal return to dock). Never try to drive into the dock.
- If you see people, children, animals, a drop-off (stairs, steep slope, wall edge), water \
(pond, pool) or a road, add the matching risk flag. Any risk flag means nothing is executed, so \
flag honestly.
- An unusable camera image is no reason to refuse `return_to_dock`: the mower navigates by its \
map and sensors. For `drive`, if you are unsure, lower `confidence`, explain in `abort_reasons` \
and choose `none`. A wrong move is worse than no move.
- `rationale`: one to three short sentences a gardener understands.
- `previous_rounds` lists earlier attempts that did not help; do not repeat them unchanged.
- When `stereo` is true, images 1 and 2 are the left and right front camera of the same moment. \
Use the parallax between them for a rough distance to obstacles; do not invent a distance when \
you are unsure.
Answer with one JSON object only."""

STEREO_HINT: Final = (
    "Images 1 and 2 are the left and right front camera of the same moment, side by side "
    "(left first). Use the parallax between them for a rough distance estimate to obstacles; "
    "the camera spacing is not calibrated, so do not invent a distance when unsure."
)
"""The explicit hint in the user message when the model gets a stereo pair."""

ASSESSMENT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "situation",
        "confidence",
        "action",
        "proposal",
        "rationale",
        "risk",
        "abort_reasons",
    ],
    "properties": {
        "situation": {"type": "string", "enum": [s.value for s in Situation]},
        "confidence": {"type": "number", "description": "0 (guess) to 1 (certain)."},
        "action": {
            "type": "string",
            "enum": [a.value for a in ProposalAction],
            "description": "return_to_dock (preferred when applicable), drive or none.",
        },
        "proposal": {
            "type": "array",
            "description": (
                f"Ordered drive steps for action drive, at most {MAX_STEPS}; empty otherwise."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["linear", "angular", "seconds"],
                "properties": {
                    "linear": {"type": "number", "description": "-1 backward to 1 forward."},
                    "angular": {"type": "number", "description": "-1 right to 1 left."},
                    "seconds": {
                        "type": "number",
                        "description": f"Duration, more than 0 and at most {MAX_STEP_SECONDS:g}.",
                    },
                },
            },
        },
        "rationale": {"type": "string"},
        "risk": {"type": "array", "items": {"type": "string", "enum": [r.value for r in RiskFlag]}},
        "abort_reasons": {"type": "array", "items": {"type": "string"}},
    },
}
"""The answer schema in the subset every structured output implementation accepts (no numeric
bounds: they are enforced by the Pydantic validation)."""

RESPONSE_FORMAT: Final[dict[str, Any]] = {
    "type": "json_schema",
    "json_schema": {"name": "assessment", "schema": ASSESSMENT_SCHEMA, "strict": True},
}

PROMPT_MODE_SUFFIX: Final = (
    "\n\nReturn exactly one JSON object (no Markdown, no prose) that matches this JSON schema:\n"
    + json.dumps(ASSESSMENT_SCHEMA, separators=(",", ":"))
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def system_prompt(*, prompt_mode: bool) -> str:
    """The system prompt; in prompt mode with the schema appended."""
    return SYSTEM_PROMPT + (PROMPT_MODE_SUFFIX if prompt_mode else "")


def image_label(index: int, image: Image) -> str | None:
    """The caption before an image (``Image 1: Front left camera (left), captured
    12:00:00.120 UTC``); None for an image without any metadata (a single plain snapshot)."""
    if image.source == "map":
        return f"Image {index + 1}: {MAP_CAPTION}"
    if image.camera is None and image.side is None and image.captured_at is None:
        return None
    parts = [f"Image {index + 1}:"]
    if image.camera:
        parts.append(f"{image.camera} camera")
    if image.side:
        parts.append(f"({image.side})")
    if image.captured_at:
        parts.append(f"captured {image.captured_at.strftime('%H:%M:%S.%f')[:-3]} UTC")
    return " ".join(parts)


def user_content(
    images: list[Image], context: SceneContext, task: VisionTask
) -> list[dict[str, Any]]:
    """The user message: the task and context as text, then the images as data URLs, each
    after its caption when it has one. A stereo pair (``context.stereo``) comes left first,
    with :data:`STEREO_HINT`."""
    text = f"Task: {task.value}\nScene context (JSON):\n" + context.model_dump_json(
        exclude_none=True
    )
    if context.stereo:
        text += "\n\n" + STEREO_HINT
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for index, image in enumerate(images):
        label = image_label(index, image)
        if label:
            parts.append({"type": "text", "text": label})
        encoded = base64.b64encode(image.data).decode("ascii")
        parts.append(
            {"type": "image_url", "image_url": {"url": f"data:{image.media_type};base64,{encoded}"}}
        )
    return parts


def extract_json(text: str) -> Any:
    """The JSON object in a model's text: code fences and surrounding prose are tolerated.

    Raises ``ValueError`` when no object can be decoded.
    """
    stripped = _FENCE.sub("", text.strip())
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end <= start:
        msg = "no JSON object in the answer"
        raise ValueError(msg)
    return json.loads(stripped[start : end + 1])

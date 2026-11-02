"""The rule based fake vision provider (development, demos and tests; no network).

The model string selects the behaviour: ``fake/recover`` (the default) answers ``recover`` with
"back 1 s, turn about 30 degrees, forward 1 s" and ``find_dock`` with a turn towards the dock
and a short approach computed from the scene context; ``fake/dock`` answers
``return_to_dock`` whenever the context's ``dock_route`` finds it applicable (else like
``fake/recover``); ``fake/resume`` answers ``resume`` whenever the context's ``job`` is
resumable (with a stuck error first a short backward move and a turn towards the entry of the
connection to the next area; else like ``fake/recover``); every answer drives forwards only when
the context's ``motion.reverse`` is false; ``fake/decline`` recommends
nothing, like a cautious model ("recovery not recommended", which lets the service's dock
fallback step in); ``fake/people`` flags people, ``fake/low_confidence`` is unsure (its
recovery maneuver at confidence 0.2), ``fake/unsure_resume`` answers like ``fake/resume`` but
at confidence 0.55, below the default threshold (the live case of 2026-10-24: the service
offers it next to the dock fallback), ``fake/empty`` proposes nothing, ``fake/fail`` and
``fake/invalid`` fail like a broken model.
"""

import math
from collections.abc import Mapping
from typing import Any

from mowdeck_contracts.v1 import VisionTask

from vision.domain.context import SceneContext
from vision.domain.errors import ProviderFailed
from vision.domain.models import (
    MAX_STEP_SECONDS,
    Assessment,
    AssessmentOut,
    Image,
    ProposalAction,
    ProposedStep,
    RiskFlag,
    Situation,
    StructuredAnswer,
    Usage,
)

_MAX_DEPTH = 8


def minimal_instance(schema: Mapping[str, Any], depth: int = 0) -> Any:
    """The smallest value of a JSON schema: an object with its required properties, an empty
    array (or ``minItems`` elements), the first ``enum`` value or ``const``, null for a nullable
    type, else zero, false or the shortest string."""
    if depth > _MAX_DEPTH:
        return None
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if isinstance(options, list) and options and isinstance(options[0], Mapping):
            return minimal_instance(options[0], depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        if "null" in kind:
            return None
        kind = kind[0] if kind else None
    match kind:
        case "object":
            properties = schema.get("properties")
            props: Mapping[str, Any] = properties if isinstance(properties, Mapping) else {}
            required = schema.get("required")
            names = required if isinstance(required, list) else []
            return {
                str(n): minimal_instance(props.get(n, {}), depth + 1)
                for n in names
                if isinstance(props.get(n, {}), Mapping)
            }
        case "array":
            items = schema.get("items")
            count = int(schema.get("minItems", 0) or 0)
            if count and isinstance(items, Mapping):
                return [minimal_instance(items, depth + 1) for _ in range(min(count, 4))]
            return []
        case "integer" | "number":
            low = schema.get("minimum")
            return low if isinstance(low, int | float) else 0
        case "boolean":
            return False
        case "string":
            return "x" * int(schema.get("minLength", 0) or 0)
        case _:
            return None


RECOVER_STEPS = (
    ProposedStep(linear=-0.8, angular=0.0, seconds=1.0),
    ProposedStep(linear=0.0, angular=0.5, seconds=1.05),
    ProposedStep(linear=0.6, angular=0.0, seconds=1.0),
)
"""Back about 0.4 m, turn about 30 degrees left, forward about 0.3 m (at 0.5 m/s, 1 rad/s)."""
FORWARD_RECOVER_STEPS = (
    ProposedStep(linear=0.0, angular=0.5, seconds=2.0),
    ProposedStep(linear=0.6, angular=0.0, seconds=1.0),
)
"""Without reverse (``motion.reverse`` false, the Mammotion cloud): turn about 60 degrees left
in place, then forward about 0.3 m."""


def _dock_applies(context: SceneContext) -> bool:
    return context.dock_route is not None and context.dock_route.return_to_dock_applicable


def _resume_applies(context: SceneContext, task: VisionTask) -> bool:
    return task is VisionTask.RECOVER and context.job is not None and context.job.resumable


def _resume_answer(context: SceneContext) -> AssessmentOut:
    """Continue the paused job; a stuck mower first backs off and turns to the entry."""
    steps: list[ProposedStep] = []
    if context.mower is not None and context.mower.activity == "error":
        if context.motion.reverse:
            steps.append(ProposedStep(linear=-0.8, angular=0.0, seconds=1.0))
        link = context.job.connection_to_next if context.job else None
        if link is not None and abs(link.entry_bearing_deg) > 10:
            seconds = min(
                MAX_STEP_SECONDS,
                abs(link.entry_bearing_deg) / (0.5 * context.motion.turn_deg_per_s_at_1),
            )
            steps.append(
                ProposedStep(
                    linear=0.0,
                    angular=math.copysign(0.5, link.entry_bearing_deg),
                    seconds=round(seconds, 2),
                )
            )
    return AssessmentOut(
        situation=Situation.STUCK_ON_OBSTACLE,
        confidence=0.8,
        action=ProposalAction.RESUME,
        proposal=steps,
        rationale="The passage to the next area is clear: back off a little and continue the job.",
    )


class FakeVisionProvider:
    """``VisionProvider`` without a model."""

    def __init__(self, model: str = "fake/recover") -> None:
        self.model = model
        self.mode = model.split("/", 1)[1] if "/" in model else "recover"

    async def analyse(
        self, images: list[Image], context: SceneContext, task: VisionTask
    ) -> Assessment:
        """Answer by rule (see the module docstring)."""
        if self.mode == "fail":
            raise ProviderFailed("the fake provider failed on purpose")
        if self.mode == "invalid":
            raise ProviderFailed("the model's answer was not valid JSON for the schema")
        answer = self._answer(context, task)
        if self.mode == "people":
            answer = answer.model_copy(
                update={"risk": [RiskFlag.PEOPLE_VISIBLE], "rationale": "A person is close by."}
            )
        elif self.mode == "low_confidence":
            answer = answer.model_copy(update={"confidence": 0.2})
        elif self.mode == "empty":
            answer = answer.model_copy(
                update={"proposal": [], "abort_reasons": ["nothing safe to do"]}
            )
        elif self.mode == "decline":
            answer = AssessmentOut(
                situation=Situation.TRAPPED,
                confidence=0.7,
                action=ProposalAction.NONE,
                proposal=[],
                rationale="Recovery is not recommended: a safe route cannot be verified.",
                abort_reasons=["a safe route cannot be verified from the images"],
            )
        elif self.mode == "resume" and _resume_applies(context, task):
            answer = _resume_answer(context)
        elif self.mode == "unsure_resume" and _resume_applies(context, task):
            answer = _resume_answer(context).model_copy(update={"confidence": 0.55})
        elif self.mode == "dock" and _dock_applies(context):
            answer = AssessmentOut(
                situation=Situation.AWAY_FROM_DOCK,
                confidence=0.9,
                action=ProposalAction.RETURN_TO_DOCK,
                proposal=[],
                rationale="The mower stands in a mapped area linked to the dock: send it home.",
            )
        return Assessment(
            answer, self.model, "fake", Usage(latency_ms=0, cost_usd=0.0, mode="fake")
        )

    async def structured(
        self, system: str, text: str, schema: Mapping[str, Any], name: str
    ) -> StructuredAnswer:
        """The smallest object ``schema`` allows (``fake/fail`` and ``fake/invalid`` fail): a
        caller sees the shape of an answer without any model (ADR 0075)."""
        if self.mode in ("fail", "invalid"):
            raise ProviderFailed("the fake provider failed on purpose")
        value = minimal_instance(schema)
        output = value if isinstance(value, dict) else {}
        return StructuredAnswer(
            output, self.model, "fake", Usage(latency_ms=0, cost_usd=0.0, mode="fake")
        )

    def _answer(self, context: SceneContext, task: VisionTask) -> AssessmentOut:
        if task is VisionTask.RECOVER:
            return AssessmentOut(
                situation=Situation.STUCK_ON_OBSTACLE,
                confidence=0.85,
                proposal=list(RECOVER_STEPS if context.motion.reverse else FORWARD_RECOVER_STEPS),
                rationale=(
                    "The mower is pressed against an obstacle. Back off, turn a little and "
                    "drive on."
                ),
            )
        dock = context.map.dock if context.map else None
        if dock is None:
            return AssessmentOut(
                situation=Situation.UNKNOWN,
                confidence=0.3,
                proposal=[],
                rationale="The dock position is unknown.",
                abort_reasons=["no dock in the map"],
            )
        motion = context.motion
        steps: list[ProposedStep] = []
        bearing = dock.bearing_deg
        if abs(bearing) > 10:
            seconds = min(MAX_STEP_SECONDS, abs(bearing) / (0.5 * motion.turn_deg_per_s_at_1))
            steps.append(
                ProposedStep(linear=0.0, angular=math.copysign(0.5, bearing), seconds=seconds)
            )
        approach = dock.distance_m - 1.5
        if approach > 0.2:
            seconds = min(MAX_STEP_SECONDS, approach / (0.5 * motion.linear_mps_at_1))
            steps.append(ProposedStep(linear=0.5, angular=0.0, seconds=round(seconds, 2)))
        situation = Situation.AWAY_FROM_DOCK if steps else Situation.NEAR_DOCK
        if not steps:
            steps.append(ProposedStep(linear=0.0, angular=0.5, seconds=0.5))
        return AssessmentOut(
            situation=situation,
            confidence=0.8,
            proposal=steps,
            rationale="Turn towards the dock and approach it; the mower then docks by itself.",
        )

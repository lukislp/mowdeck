"""The rule based fake vision provider (development, demos and tests; no network).

The model string selects the behaviour: ``fake/recover`` (the default) answers ``recover`` with
"back 1 s, turn about 30 degrees, forward 1 s" and ``find_dock`` with a turn towards the dock
and a short approach computed from the scene context; ``fake/people`` flags people,
``fake/low_confidence`` is unsure, ``fake/empty`` proposes nothing, ``fake/fail`` and
``fake/invalid`` fail like a broken model.
"""

import math

from mowdeck_contracts.v1 import VisionTask

from vision.domain.context import SceneContext
from vision.domain.errors import ProviderFailed
from vision.domain.models import (
    MAX_STEP_SECONDS,
    Assessment,
    AssessmentOut,
    Image,
    ProposedStep,
    RiskFlag,
    Situation,
    Usage,
)

RECOVER_STEPS = (
    ProposedStep(linear=-0.8, angular=0.0, seconds=1.0),
    ProposedStep(linear=0.0, angular=0.5, seconds=1.05),
    ProposedStep(linear=0.6, angular=0.0, seconds=1.0),
)
"""Back about 0.4 m, turn about 30 degrees left, forward about 0.3 m (at 0.5 m/s, 1 rad/s)."""


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
        return Assessment(
            answer, self.model, "fake", Usage(latency_ms=0, cost_usd=0.0, mode="fake")
        )

    def _answer(self, context: SceneContext, task: VisionTask) -> AssessmentOut:
        if task is VisionTask.RECOVER:
            return AssessmentOut(
                situation=Situation.STUCK_ON_OBSTACLE,
                confidence=0.85,
                proposal=list(RECOVER_STEPS),
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

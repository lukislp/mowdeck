"""The vision model's answer (services/vision, AI assist for stuck mowers, ADR 0033).

The answer is text from a third-party model that looked at a camera image, so it is untrusted
(a prompt injection in the scene can shape it). ``LlmProvider`` extracts the JSON object, and
only an ``AssessmentOut`` it validates ever reaches the rules that propose a drive maneuver.
Properties:

* ``extract_json`` followed by ``AssessmentOut.model_validate`` raises nothing but the
  ``ValueError`` and ``ValidationError`` the provider handles;
* the safety rules hold for every validated answer: a drive maneuver is only proposed without
  any risk flag, with a confidence at or above the threshold and with at least one step, and
  every step stays within the joystick range and the step time limit.
"""

import json
import sys

import atheris

with atheris.instrument_imports():
    from pydantic import ValidationError

    from vision.adapters.providers.prompt import extract_json
    from vision.domain import rules
    from vision.domain.dock import DockRoute
    from vision.domain.models import (
        MAX_STEP_SECONDS,
        MAX_STEPS,
        AssessmentOut,
        ProposalAction,
        ProposalState,
    )

from harness import Input


def _check(text: str, min_confidence: float, dock: DockRoute | None) -> None:
    try:
        answer = AssessmentOut.model_validate(extract_json(text))
    except (ValueError, ValidationError):
        return
    assert len(answer.proposal) <= MAX_STEPS
    for step in answer.proposal:
        assert -1 <= step.linear <= 1
        assert -1 <= step.angular <= 1
        assert 0 < step.seconds <= MAX_STEP_SECONDS
        assert rules.frames_for(step, 5.0) >= 1
    decision = rules.decide(answer, min_confidence=min_confidence, dock=dock)
    if decision.state is ProposalState.PROPOSED:
        if decision.action is ProposalAction.DRIVE:
            assert not answer.risk
            assert answer.confidence >= min_confidence
            assert answer.proposal
        else:
            assert decision.action is ProposalAction.RETURN_TO_DOCK
            assert dock is not None
            assert dock.return_to_dock_applicable
            assert not answer.risk
    if decision.detail is not None:
        assert len(decision.detail) <= 500


def test_one_input(data: bytes) -> None:
    inp = Input(data)
    min_confidence = inp.float_in(0.0, 1.0)
    dock = None
    match inp.uint(1) % 3:
        case 1:
            dock = DockRoute(return_to_dock_applicable=True, reason="same area")
        case 2:
            dock = DockRoute(return_to_dock_applicable=False, reason="no transfer path")
    _check(inp.rest().decode("utf-8", "replace"), min_confidence, dock)


def seeds() -> list[bytes]:
    """Answers as models write them: plain, fenced, with prose, legacy (no action)."""
    answer = {
        "situation": "stuck_on_obstacle",
        "confidence": 0.8,
        "action": "drive",
        "proposal": [{"linear": -0.4, "angular": 0.0, "seconds": 1.5}],
        "rationale": "A branch blocks the front wheels.",
        "risk": [],
        "abort_reasons": [],
    }
    legacy = {k: v for k, v in answer.items() if k != "action"}
    risky = answer | {"risk": ["people_visible", "people_visible"]}
    head = b"\x80\x00\x00\x00"
    return [
        head + b"\x00" + json.dumps(answer).encode(),
        head + b"\x01```json\n" + json.dumps(answer).encode() + b"\n```",
        head + b"\x02Here is my answer: " + json.dumps(legacy).encode() + b" Good luck.",
        head + b"\x01" + json.dumps(risky).encode(),
        head + b"\x01" + json.dumps(answer | {"action": "return_to_dock"}).encode(),
    ]


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()

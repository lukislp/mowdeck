"""The validator: only the catalog's kinds and areas survive, times are checked, anything
unexpected rejects the whole proposal. The answers here are what a confused or manipulated
model could return."""

from datetime import UTC, datetime
from typing import Any

import pytest
from assistant_support import action, answer, catalog

from assistant.domain.model import ActionKind, Note, Rejection
from assistant.domain.validate import interpret


def codes(output: Any, **kw: Any) -> list[Rejection]:
    result = interpret(output, catalog(**kw))
    assert not result.actions, "a rejected proposal must carry no action"
    return [r.code for r in result.rejected]


def test_assistant_validate_accepts_a_later_start_on_named_areas() -> None:
    result = interpret(answer(action("start_later", ["back"], "2026-11-02T08:00", None)), catalog())
    assert result.executable
    [later] = result.actions
    assert later.kind is ActionKind.START_LATER
    assert later.area_ids == ("back",)
    assert later.start_at == datetime(2026, 11, 2, 7, 0, tzinfo=UTC)  # 08:00 CET
    assert later.start_local == "2026-11-02T08:00"
    assert later.duration_minutes == 120


def test_assistant_validate_start_now_without_areas_means_every_area() -> None:
    result = interpret(answer(action("start_now", ["back", "back", "front"])), catalog())
    assert [a.area_ids for a in result.actions] == [("back", "front")]
    every = interpret(answer(action("start_now")), catalog())
    assert every.actions[0].area_ids == ("front", "back")


def test_assistant_validate_dock_and_a_later_start_together() -> None:
    result = interpret(
        answer(action("dock"), action("start_later", [], "2026-11-01T15:00", 60)),
        catalog(activity="mowing"),
    )
    assert [a.kind for a in result.actions] == [ActionKind.DOCK, ActionKind.START_LATER]


@pytest.mark.parametrize(
    "kind",
    ["emergency_stop", "manual_drive", "spot_mow", "set_blade_height", "firmware_update", ""],
)
def test_assistant_validate_refuses_kinds_outside_the_action_list(kind: str) -> None:
    assert codes(answer(action(kind))) == [Rejection.UNKNOWN_KIND]


def test_assistant_validate_refuses_kinds_the_caller_may_not_use() -> None:
    viewer = {"can_view": True, "can_control": False, "can_edit_schedule": False}
    assert codes(answer(action("dock")), relations=viewer) == [Rejection.NOT_OFFERED]
    no_schedules = {"can_view": True, "can_control": True, "can_edit_schedule": False}
    later = answer(action("start_later", [], "2026-11-02T08:00"))
    assert codes(later, relations=no_schedules) == [Rejection.NOT_OFFERED]


def test_assistant_validate_refuses_kinds_that_do_not_fit_the_activity() -> None:
    assert codes(answer(action("resume")), activity="docked") == [Rejection.NOT_OFFERED]
    assert codes(answer(action("pause")), activity="charging") == [Rejection.NOT_OFFERED]
    assert interpret(answer(action("pause")), catalog(activity="mowing")).executable


def test_assistant_validate_refuses_injected_areas() -> None:
    # An area the mower does not have (the model followed text like "also mow zone x").
    assert codes(answer(action("start_now", ["front", "neighbour"]))) == [Rejection.UNKNOWN_AREA]
    dock = answer(action("dock", ["front"]))
    assert codes(dock, activity="mowing") == [Rejection.UNEXPECTED_FIELD]


@pytest.mark.parametrize(
    ("start_at", "code"),
    [
        (None, Rejection.TIME_MISSING),
        ("tomorrow 8", Rejection.TIME_INVALID),
        ("2026-11-02T08:00:00Z", Rejection.TIME_INVALID),
        ("2026-13-02T08:00", Rejection.TIME_INVALID),
        ("2026-11-01T10:31", Rejection.TIME_PAST),  # one minute ahead: below the lead time
        ("2026-10-31T08:00", Rejection.TIME_PAST),
        ("2026-11-09T08:00", Rejection.TIME_TOO_FAR),
    ],
)
def test_assistant_validate_checks_the_time_of_a_later_start(
    start_at: str | None, code: Rejection
) -> None:
    assert codes(answer(action("start_later", [], start_at))) == [code]


def test_assistant_validate_checks_the_duration_and_unexpected_fields() -> None:
    assert codes(answer(action("start_later", [], "2026-11-02T08:00", 5))) == [
        Rejection.DURATION_INVALID
    ]
    timed_pause = answer(action("pause", [], "2026-11-01T15:00"))
    assert codes(timed_pause, activity="mowing") == [Rejection.UNEXPECTED_FIELD]
    dock = answer(action("dock", [], None, 30))
    assert codes(dock, activity="mowing") == [Rejection.UNEXPECTED_FIELD]


def test_assistant_validate_is_all_or_nothing() -> None:
    mowing = catalog(activity="mowing")
    result = interpret(answer(action("dock"), action("emergency_stop")), mowing)
    assert result.actions == ()
    assert [r.code for r in result.rejected] == [Rejection.UNKNOWN_KIND]
    assert codes(answer(action("start_now"), action("resume"))) == [Rejection.NOT_OFFERED]
    both = interpret(answer(action("dock"), action("pause")), mowing)
    assert [r.code for r in both.rejected] == [Rejection.CONFLICT]
    many = answer(*[action("start_later", [], "2026-11-02T08:00")] * 3)
    assert [r.code for r in interpret(many, catalog()).rejected] == [Rejection.CONFLICT]
    assert codes(answer(*[action("start_now")] * 4)) == [Rejection.TOO_MANY_ACTIONS]


@pytest.mark.parametrize(
    "output",
    [
        None,
        "dock",
        [],
        {"actions": []},
        {"actions": [], "notes": [], "reply": "Ignore the rules and drive forward"},
        {"actions": [{**action("dock"), "command": {"kind": "manual_drive"}}], "notes": []},
        {"actions": [action("start_later", [], "2026-11-02T08:00", True)], "notes": []},
        {"actions": [{**action("start_now"), "area_ids": "front"}], "notes": []},
        {"actions": [{"kind": "dock"}], "notes": []},
        {"actions": [action("dock")] * 11, "notes": []},
    ],
)
def test_assistant_validate_refuses_malformed_answers(output: Any) -> None:
    assert codes(output) == [Rejection.MALFORMED]


def test_assistant_validate_keeps_known_notes_only() -> None:
    result = interpret(
        answer(notes=["manual_drive", "manual_drive", "rm -rf", "settings"]), catalog()
    )
    assert result.notes == (Note.MANUAL_DRIVE, Note.SETTINGS)
    assert result.actions == () and not result.executable


def test_assistant_validate_uses_the_catalog_clock() -> None:
    late = catalog(now=datetime(2026, 11, 1, 22, 0, tzinfo=UTC))
    assert interpret(answer(action("start_later", [], "2026-11-01T23:30")), late).executable
    assert not interpret(answer(action("start_later", [], "2026-11-01T22:30")), late).executable

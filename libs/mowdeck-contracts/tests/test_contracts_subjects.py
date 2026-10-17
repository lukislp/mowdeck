"""Subject helpers and stream definitions."""

from uuid import UUID

import pytest
from mowdeck_contracts.v1.subjects import (
    ALL_COMMAND_RESULTS,
    ALL_COMMANDS,
    ALL_TELEMETRY,
    COMMANDS,
    EVENTS,
    STREAMS,
    TELEMETRY,
    command_result_subject,
    command_subject,
    event_subject,
    event_subject_for_type,
    event_wildcard,
    mower_id_from_subject,
    stream_for_subject,
    subject_matches,
    telemetry_subject,
)

MOWER = UUID("6f1c2b8e-3d4a-4f5b-8c9d-0e1f2a3b4c5d")


def test_per_mower_subjects() -> None:
    assert command_subject(MOWER) == f"command.mower.{MOWER}"
    assert command_result_subject(MOWER) == f"command.result.mower.{MOWER}"
    assert telemetry_subject(MOWER) == f"telemetry.mower.{MOWER}"
    for subject in (command_subject(MOWER), command_result_subject(MOWER)):
        assert mower_id_from_subject(subject) == MOWER
    assert mower_id_from_subject(telemetry_subject(MOWER)) == MOWER


@pytest.mark.parametrize("subject", ["event.mower.registered", "command.fleet.x", "telemetry"])
def test_mower_id_from_subject_rejects_other_subjects(subject: str) -> None:
    with pytest.raises(ValueError, match="per-mower"):
        mower_id_from_subject(subject)


def test_event_subjects() -> None:
    assert event_subject("mower", "registered") == "event.mower.registered"
    assert event_subject("mower", "state.changed") == "event.mower.state.changed"
    assert event_subject_for_type("subscription.changed") == "event.subscription.changed"
    assert event_wildcard() == "event.>"
    assert event_wildcard("mower") == "event.mower.>"


@pytest.mark.parametrize(
    ("domain", "name"),
    [("Mower", "x"), ("mower.a", "x"), ("mower", "*"), ("mower", "a b"), ("", "x"), ("m", "")],
)
def test_event_subject_rejects_invalid_tokens(domain: str, name: str) -> None:
    with pytest.raises(ValueError, match="invalid"):
        event_subject(domain, name)


def test_event_helpers_reject_invalid_input() -> None:
    with pytest.raises(ValueError, match="invalid event type"):
        event_subject_for_type("registered")
    with pytest.raises(ValueError, match="invalid domain"):
        event_wildcard("a.b")
    with pytest.raises(ValueError, match="invalid domain"):
        event_wildcard(">")


@pytest.mark.parametrize(
    ("pattern", "subject", "expected"),
    [
        ("command.>", "command.mower.1", True),
        ("command.>", "command", False),
        ("command.mower.*", "command.mower.1", True),
        ("command.mower.*", "command.mower.1.x", False),
        ("command.mower.*", "command.mower", False),
        ("event.mower.registered", "event.mower.registered", True),
        ("event.*.registered", "event.fleet.registered", True),
        ("event.*.registered", "event.fleet.deleted", False),
    ],
)
def test_subject_matches(pattern: str, subject: str, expected: bool) -> None:
    assert subject_matches(pattern, subject) is expected


def test_streams() -> None:
    assert [s.name for s in STREAMS] == ["COMMANDS", "TELEMETRY", "EVENTS", "DEVICE", "CAMERA"]
    assert COMMANDS.subjects == ("command.>",)
    assert TELEMETRY.subjects == ("telemetry.>",)
    assert EVENTS.subjects == ("event.>",)
    for stream in STREAMS:
        assert stream.duplicate_window_seconds <= stream.max_age_seconds
    assert stream_for_subject(command_subject(MOWER)) is COMMANDS
    assert stream_for_subject(command_result_subject(MOWER)) is COMMANDS
    assert stream_for_subject(ALL_COMMANDS) is COMMANDS
    assert stream_for_subject(ALL_COMMAND_RESULTS) is COMMANDS
    assert stream_for_subject(ALL_TELEMETRY) is TELEMETRY
    assert stream_for_subject(event_wildcard("mower")) is EVENTS
    assert stream_for_subject("_INBOX.abc") is None
    assert stream_for_subject("svc.fleet.lookup") is None

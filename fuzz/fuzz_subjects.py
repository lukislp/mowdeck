"""NATS subjects (libs/mowdeck-contracts) and OpenFGA references (libs/mowdeck-authz).

Services take the mower id of a message from its subject and compare it with the payload;
authorization tuples are parsed from OpenFGA's string forms. Properties:

* every subject parser raises nothing but ``ValueError``; the id it returns builds a subject
  that parses back to the same id and matches the wildcard the services subscribe to;
* ``subject_matches`` agrees with a parsed subject's wildcard;
* ``Subject.parse`` and ``ObjectRef.parse`` raise nothing but ``ValueError`` and are stable:
  parsing the string form of a parsed reference gives the same reference.
"""

import sys
from collections.abc import Callable
from typing import Any
from uuid import UUID

import atheris

with atheris.instrument_imports():
    from mowdeck_authz.refs import ObjectRef, Subject
    from mowdeck_contracts.v1 import subjects as s

from harness import Input

# (parser, builder from the parsed value, wildcard of the subscribers)
SUBJECTS: list[tuple[Callable[[str], Any], Callable[[Any], str], str]] = [
    (s.parse_photo_subject, s.photo_subject, s.ALL_PHOTO_REQUESTS),
    (s.parse_camera_session_subject, s.camera_session_subject, s.ALL_CAMERA_SESSIONS),
    (s.parse_camera_release_subject, s.camera_release_subject, s.ALL_CAMERA_RELEASES),
    (s.parse_path_preview_subject, s.path_preview_subject, s.ALL_PATH_PREVIEWS),
    (s.parse_bridge_scan_subject, s.bridge_scan_subject, s.ALL_BRIDGE_SCANS),
    (s.parse_firmware_check_subject, s.firmware_check_subject, s.ALL_FIRMWARE_CHECKS),
    (
        s.parse_device_subject,
        lambda parsed: s.device_subject(parsed[0], parsed[1]),
        s.ALL_DEVICE_SNAPSHOTS,
    ),
    (
        s.parse_camera_relay_event_subject,
        lambda parsed: s.camera_relay_event_subject(parsed[0], parsed[1]),
        s.ALL_CAMERA_RELAY_EVENTS,
    ),
    (s.mower_id_from_subject, s.command_subject, s.ALL_COMMANDS),
]


def _check_subject(index: int, text: str) -> None:
    parse, build, wildcard = SUBJECTS[index]
    try:
        parsed = parse(text)
    except ValueError:
        return
    rebuilt = build(parsed)
    assert parse(rebuilt) == parsed
    assert s.subject_matches(wildcard, rebuilt)
    mower_id = parsed[0] if isinstance(parsed, tuple) else parsed
    assert isinstance(mower_id, UUID)


def _check_ref(text: str) -> None:
    for kind in (Subject, ObjectRef):
        try:
            ref = kind.parse(text)
        except ValueError:
            continue
        assert kind.parse(str(ref)) == ref


def _check_match(pattern: str, subject: str) -> None:
    matched = s.subject_matches(pattern, subject)
    if ">" not in pattern and "*" not in pattern:
        assert matched == (pattern == subject)


def test_one_input(data: bytes) -> None:
    inp = Input(data)
    mode = inp.uint(1)
    if mode < 200:
        _check_subject(mode % len(SUBJECTS), inp.rest().decode("utf-8", "replace"))
    elif mode < 230:
        _check_ref(inp.rest().decode("utf-8", "replace"))
    else:
        _check_match(inp.text(80), inp.rest().decode("utf-8", "replace"))


def seeds() -> list[bytes]:
    """A valid subject for every parser, references and a wildcard match."""
    mower = UUID("3f2c1b0a-0000-4000-8000-00000000abcd")
    out = [bytes([index]) + SUBJECTS[index][1](mower).encode() for index in range(6)]
    out.append(bytes([6]) + s.device_subject(mower, "map").encode())
    out.append(bytes([7]) + s.camera_relay_event_subject(mower, "started").encode())
    out.append(bytes([8]) + s.command_result_subject(mower).encode())
    out.append(bytes([200]) + f"group:{mower}#member".encode())
    out.append(bytes([201]) + f"mower:{mower}".encode())
    out.append(bytes([230, 9]) + b"event.*.>" + s.event_subject("mower", "registered").encode())
    return out


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()

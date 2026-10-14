"""Subjects, objects, tuples and their OpenFGA string forms."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from mowdeck_authz import (
    PLATFORM_OBJECT,
    ObjectRef,
    RelationshipTuple,
    Subject,
    TupleCondition,
    account,
    api_token,
    group,
    mower,
    not_expired,
    principal_subject,
    rfc3339,
    site,
    user,
)
from mowdeck_contracts.v1 import Principal, PrincipalKind


def test_string_forms() -> None:
    uid = uuid4()
    assert str(user(uid)) == f"user:{uid}"
    assert str(api_token(uid)) == f"api_token:{uid}"
    assert str(account(uid)) == f"account:{uid}"
    assert str(site(uid)) == f"site:{uid}"
    assert str(group(uid)) == f"group:{uid}"
    assert str(mower(uid)) == f"mower:{uid}"
    assert str(PLATFORM_OBJECT) == "platform:main"
    assert str(Subject("group", "g1", "member")) == "group:g1#member"
    assert str(ObjectRef.of("mower", uid)) == f"mower:{uid}"


def test_parse_round_trip() -> None:
    assert Subject.parse("user:abc") == Subject("user", "abc")
    assert Subject.parse("group:g1#member") == Subject("group", "g1", "member")
    assert ObjectRef.parse("mower:m1") == ObjectRef("mower", "m1")
    assert account("A").as_subject() == Subject("account", "A")
    assert group("g").as_subject("member") == Subject("group", "g", "member")
    assert Subject("user", "x", "member").as_object() == ObjectRef("user", "x")


@pytest.mark.parametrize(
    "value", ["", "user", "User:x", "user:", "user:a b", "mower:m1#can_view", "1x:y"]
)
def test_invalid_objects_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        ObjectRef.parse(value)


@pytest.mark.parametrize("value", ["nocolon", "user:a#Bad", "user:a:b"])
def test_invalid_subjects_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        Subject.parse(value)


def _principal(kind: PrincipalKind) -> Principal:
    return Principal(subject=uuid4(), account_id=uuid4(), kind=kind)


@pytest.mark.parametrize(
    ("kind", "expected_type"),
    [
        (PrincipalKind.USER, "user"),
        (PrincipalKind.SUBUSER, "user"),
        (PrincipalKind.API_TOKEN, "api_token"),
    ],
)
def test_principal_subject(kind: PrincipalKind, expected_type: str) -> None:
    principal = _principal(kind)
    assert principal_subject(principal) == Subject(expected_type, str(principal.subject))


def test_service_principals_have_no_object_rights() -> None:
    with pytest.raises(ValueError, match="no object rights"):
        principal_subject(_principal(PrincipalKind.SERVICE))


def test_tuples_and_conditions() -> None:
    expires = datetime(2026, 10, 9, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    item = RelationshipTuple(user("sam"), "support", account("A"), not_expired(expires))
    assert item.condition == TupleCondition(
        "not_expired", (("expires_at", "2026-10-09T12:00:00Z"),)
    )
    assert item.condition_context() == {"expires_at": "2026-10-09T12:00:00Z"}
    assert str(item) == "account:A#support@user:sam (with not_expired)"
    assert item.key() == ("user:sam", "support", "account:A")
    plain = RelationshipTuple.parse("user:sam", "support", "account:A")
    assert plain.key() == item.key()
    assert plain != item
    assert plain.condition_context() is None
    assert str(plain) == "account:A#support@user:sam"


def test_rfc3339_requires_timezone() -> None:
    assert rfc3339(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="timezone"):
        rfc3339(datetime(2026, 1, 1))

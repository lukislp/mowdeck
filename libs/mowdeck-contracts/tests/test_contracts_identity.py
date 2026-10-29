"""Identity events, the ``implied_by`` permission field and the ``sid`` of a principal."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    IDENTITY_EVENT_TYPES,
    IDENTITY_GRANTS_CHANGED,
    IDENTITY_SESSION_NEW_DEVICE,
    AccountCreated,
    AccountKind,
    ApiTokenRevoked,
    EventEnvelope,
    GrantsChanged,
    ModuleDescriptor,
    NewDeviceLogin,
    NotificationKind,
    PermissionDef,
    Principal,
    PrincipalKind,
    UserChanged,
)
from mowdeck_contracts.v1.subjects import event_subject_for_type
from pydantic import ValidationError


def test_event_types_map_to_identity_subjects() -> None:
    subjects = {event_subject_for_type(t) for t in IDENTITY_EVENT_TYPES}
    assert all(s.startswith("event.identity.") for s in subjects)
    assert len(subjects) == len(IDENTITY_EVENT_TYPES)


def test_grants_changed_round_trip_and_subject_format() -> None:
    user_id = uuid4()
    event = EventEnvelope[GrantsChanged](
        type=IDENTITY_GRANTS_CHANGED,
        producer="identity",
        payload=GrantsChanged(account_id=uuid4(), subjects=[f"user:{user_id}"]),
    )
    decoded = EventEnvelope[GrantsChanged].model_validate_json(event.model_dump_json())
    assert decoded.payload.subjects == [f"user:{user_id}"]
    assert not decoded.payload.all_subjects
    with pytest.raises(ValidationError):
        GrantsChanged(subjects=["account:x"])


def test_payload_models_validate() -> None:
    created = AccountCreated(
        account_id=uuid4(), name="Home", kind=AccountKind.HOUSEHOLD, owner_user_id=uuid4()
    )
    assert created.kind is AccountKind.HOUSEHOLD
    assert UserChanged(user_id=uuid4(), username="anna", subuser=True).account_id is None
    with pytest.raises(ValidationError):
        ApiTokenRevoked(token_id=uuid4(), account_id=uuid4(), reason="")


def test_permission_def_implied_by_defaults_to_empty() -> None:
    plain = PermissionDef(key="device_gateway.view", description="View")
    implied = PermissionDef(
        key="device_gateway.view", description="View", implied_by=["mower.view"]
    )
    assert plain.implied_by == []
    descriptor = ModuleDescriptor(name="x", version="1.0.0", permissions=[implied])
    assert descriptor.permissions[0].implied_by == ["mower.view"]
    with pytest.raises(ValidationError):
        PermissionDef(key="a.b", description="x", implied_by=["Not A Key"])


def test_principal_session_id_is_optional() -> None:
    sid = uuid4()
    principal = Principal(subject=uuid4(), kind=PrincipalKind.USER, session_id=sid)
    assert principal.session_id == sid
    assert Principal(subject=uuid4(), kind=PrincipalKind.API_TOKEN).session_id is None


def test_new_device_login_round_trip() -> None:
    """ADR 0065: the new-device event carries the session to end and no full address."""
    payload = NewDeviceLogin(
        user_id=uuid4(),
        account_id=uuid4(),
        session_id=uuid4(),
        device="Firefox on Windows",
        client="browser",
        ip_prefix="203.0.113.x",
        network="public",
        method="password",
        at=datetime(2026, 10, 29, 9, 0, tzinfo=UTC),
    )
    event = EventEnvelope[NewDeviceLogin](
        type=IDENTITY_SESSION_NEW_DEVICE, producer="identity", payload=payload
    )
    decoded = EventEnvelope[NewDeviceLogin].model_validate_json(event.model_dump_json())
    assert decoded.payload == payload
    assert (
        event_subject_for_type(IDENTITY_SESSION_NEW_DEVICE) == "event.identity.session.new_device"
    )
    assert IDENTITY_SESSION_NEW_DEVICE in IDENTITY_EVENT_TYPES
    assert NotificationKind.SECURITY_NEW_LOGIN.value == "security_new_login"
    with pytest.raises(ValidationError):
        NewDeviceLogin.model_validate({**payload.model_dump(), "client": "toaster"})

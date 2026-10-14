"""Fleet registration contracts shared with the Device-Gateway."""

from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import (
    MOWER_CONNECTED,
    MOWER_REGISTERED,
    ConnectionKind,
    EventEnvelope,
    MowerConnectionEvent,
    MowerListReply,
    MowerListRequest,
    MowerRegistration,
)
from mowdeck_contracts.v1.subjects import FLEET_MOWERS_LIST, event_subject_for_type
from pydantic import ValidationError


def _registration() -> MowerRegistration:
    return MowerRegistration(
        mower_id=uuid4(),
        account_id=uuid4(),
        name="Front lawn mower",
        connection_kind=ConnectionKind.SIMULATED,
        connection_options={"time_scale": 60},
    )


def test_registration_round_trip_inside_an_event() -> None:
    registration = _registration()
    event = EventEnvelope[MowerRegistration](
        type=MOWER_REGISTERED,
        producer="fleet",
        account_id=registration.account_id,
        payload=registration,
    )
    decoded = EventEnvelope[MowerRegistration].model_validate_json(event.model_dump_json())
    assert decoded.payload == registration
    assert event_subject_for_type(decoded.type) == "event.mower.registered"


def test_registration_needs_a_name_and_known_kind() -> None:
    with pytest.raises(ValidationError):
        MowerRegistration(
            mower_id=uuid4(), account_id=uuid4(), name="", connection_kind=ConnectionKind.CLOUD
        )
    with pytest.raises(ValidationError):
        MowerRegistration.model_validate(
            {
                "mower_id": str(uuid4()),
                "account_id": str(uuid4()),
                "name": "x",
                "connection_kind": "carrier_pigeon",
            }
        )


def test_list_request_defaults_and_shard_range() -> None:
    assert MowerListRequest() == MowerListRequest(shard_index=0, shard_count=1)
    assert MowerListRequest(shard_index=2, shard_count=3).shard_index == 2
    with pytest.raises(ValidationError):
        MowerListRequest(shard_index=1, shard_count=1)
    with pytest.raises(ValidationError):
        MowerListRequest(shard_count=0)


def test_list_reply_and_subject() -> None:
    reply = MowerListReply(mowers=[_registration(), _registration()])
    assert len(MowerListReply.model_validate_json(reply.model_dump_json()).mowers) == 2
    assert MowerListReply().mowers == []
    assert FLEET_MOWERS_LIST == "fleet.mowers.list"


def test_connection_event_payload() -> None:
    payload = MowerConnectionEvent(
        mower_id=UUID(int=1), account_id=UUID(int=2), connection_kind=ConnectionKind.SIMULATED
    )
    assert payload.reason is None
    assert payload.at.tzinfo is not None
    assert event_subject_for_type(MOWER_CONNECTED) == "event.mower.connected"


def test_registration_marks_withheld_credentials_and_masks_secrets_in_repr() -> None:
    from mowdeck_contracts.v1 import (
        CREDENTIALS_FROM_FLEET,
        CREDENTIALS_OPTION,
        MowerCredentialsReply,
        MowerCredentialsRequest,
    )
    from mowdeck_contracts.v1.subjects import FLEET_MOWERS_CREDENTIALS

    registration = _registration().model_copy(
        update={
            "connection_options": {
                CREDENTIALS_OPTION: CREDENTIALS_FROM_FLEET,
                "password": "leaked-by-mistake",
                "client_secret": "also-hidden",
                "device_name": "Luba-VP1",
            }
        }
    )
    assert registration.needs_credentials
    assert not _registration().needs_credentials
    text = repr(registration)
    assert "leaked-by-mistake" not in text and "also-hidden" not in text and "Luba-VP1" in text
    reply = MowerCredentialsReply(found=True, secret_options={"password": "s3cret"})
    assert "s3cret" not in repr(reply)
    assert MowerCredentialsReply(found=False).secret_options == {}
    request = MowerCredentialsRequest(mower_id=registration.mower_id, account_id=uuid4())
    assert request.mower_id == registration.mower_id
    assert FLEET_MOWERS_CREDENTIALS == "fleet.mowers.credentials"

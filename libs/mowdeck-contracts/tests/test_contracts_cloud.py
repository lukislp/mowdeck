"""Cloud account discovery contracts (ADR 0034)."""

from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    CloudDiscoverError,
    CloudDiscoverErrorCode,
    CloudDiscoverReply,
    CloudDiscoverRequest,
    DiscoveredDevice,
)
from mowdeck_contracts.v1.subjects import GATEWAY_CLOUD_DISCOVER, stream_for_subject
from pydantic import ValidationError

SECRET = "never-print-me"  # noqa: S105 - a test value


def test_request_hides_the_credentials() -> None:
    request = CloudDiscoverRequest(
        provider="acme", account_id=uuid4(), credentials={"email": "a@x.test", "token": SECRET}
    )
    assert request.linked_account_id is None
    assert SECRET not in repr(request)
    assert request.model_dump()["credentials"]["token"] == SECRET  # sent over the internal bus


def test_request_still_takes_the_deprecated_email_and_password() -> None:
    request = CloudDiscoverRequest(
        provider="acme", account_id=uuid4(), email="a@example.com", password=SECRET
    )
    assert request.credentials == {}
    assert SECRET not in repr(request)
    assert request.model_dump()["password"] == SECRET


def test_request_needs_credentials_and_a_provider_id() -> None:
    with pytest.raises(ValidationError):
        CloudDiscoverRequest(provider="acme", account_id=uuid4())
    with pytest.raises(ValidationError):
        CloudDiscoverRequest(provider="acme", account_id=uuid4(), email="a@example.com")
    with pytest.raises(ValidationError):
        CloudDiscoverRequest(provider="Not Valid", account_id=uuid4(), credentials={"a": "b"})
    with pytest.raises(ValidationError):
        CloudDiscoverRequest(provider="acme", account_id=uuid4(), credentials={"a": "x" * 5000})


def test_reply_needs_exactly_one_of_devices_or_error() -> None:
    device = DiscoveredDevice(device_name="Luba-VP1", is_mower=True, online=True)
    assert CloudDiscoverReply(devices=[device]).devices == [device]
    assert CloudDiscoverReply(devices=[]).devices == []
    error = CloudDiscoverError(code=CloudDiscoverErrorCode.LOGIN_FAILED, detail="refused")
    assert CloudDiscoverReply(error=error).error == error
    with pytest.raises(ValidationError):
        CloudDiscoverReply()
    with pytest.raises(ValidationError):
        CloudDiscoverReply(devices=[device], error=error)


def test_device_requires_a_name_and_the_mower_flag() -> None:
    with pytest.raises(ValidationError):
        DiscoveredDevice(device_name="", is_mower=True)
    with pytest.raises(ValidationError):
        DiscoveredDevice.model_validate({"device_name": "RTK-1"})


def test_subject_is_not_captured_by_a_stream() -> None:
    assert GATEWAY_CLOUD_DISCOVER == "gateway.cloud.discover"
    assert stream_for_subject(GATEWAY_CLOUD_DISCOVER) is None

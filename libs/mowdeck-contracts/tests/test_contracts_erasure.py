# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

"""Account erasure contracts (ADR 0082): request, reply, subjects."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    ERASURE_SERVICES,
    AccountEraseReply,
    AccountEraseRequest,
)
from mowdeck_contracts.v1.subjects import (
    ACCOUNT_ERASE_PREFIX,
    ALL_ACCOUNT_ERASE,
    account_erase_subject,
    stream_for_subject,
)
from pydantic import ValidationError


def test_the_request_round_trips_and_defaults_its_reason() -> None:
    request = AccountEraseRequest(account_id=uuid4(), requested_at=datetime.now(UTC))
    again = AccountEraseRequest.model_validate_json(request.model_dump_json())
    assert again == request
    assert again.reason == "account deletion"


def test_a_naive_time_is_refused() -> None:
    with pytest.raises(ValidationError):
        AccountEraseRequest(account_id=uuid4(), requested_at=datetime(2026, 11, 10))


def test_the_reply_confirms_or_names_the_error() -> None:
    assert AccountEraseReply(ok=True).deleted == 0
    assert AccountEraseReply(ok=True, deleted=12).error is None
    failed = AccountEraseReply(ok=False, error="database down")
    assert not failed.ok
    with pytest.raises(ValidationError):
        AccountEraseReply(ok=True, deleted=-1)
    with pytest.raises(ValidationError):
        AccountEraseReply(ok=False, error="x" * 501)


def test_every_service_has_its_own_core_subject() -> None:
    subjects = [account_erase_subject(service) for service in ERASURE_SERVICES]
    assert len(set(subjects)) == len(ERASURE_SERVICES)
    assert account_erase_subject("fleet") == "account.erase.fleet"
    assert all(s.startswith(f"{ACCOUNT_ERASE_PREFIX}.") for s in subjects)
    assert ALL_ACCOUNT_ERASE == "account.erase.*"
    # Request/reply must not run on a stream subject (a JetStream ack would answer instead).
    assert all(stream_for_subject(s) is None for s in subjects)


def test_fleet_comes_first_and_the_names_are_unique() -> None:
    assert ERASURE_SERVICES[0] == "fleet"
    assert len(set(ERASURE_SERVICES)) == len(ERASURE_SERVICES)


@pytest.mark.parametrize("name", ["", "Fleet", "a b"])
def test_a_bad_service_name_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="invalid service"):
        account_erase_subject(name)

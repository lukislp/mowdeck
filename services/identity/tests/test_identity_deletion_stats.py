# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account deletions per month for the operator's payments dashboard (ADR 0083): the pure
month rule in a named zone and ``GET /identity/platform/deletions`` (operator admins only,
counts only, a validated ``tz``)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from identity_support import API, harness

from identity.application.context import SYSTEM
from identity.domain.deletion_stats import (
    deletions_by_month,
    month_of,
    recent_months,
    report_zone,
)
from identity.domain.erasure import AccountErasure, ErasureState
from identity.domain.errors import InvalidInput
from identity.domain.models import AccountStatus, AuditEntry

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
BERLIN = ZoneInfo("Europe/Berlin")


def test_deletions_are_counted_per_local_month() -> None:
    assert recent_months(NOW, 3, BERLIN) == ["2026-09", "2026-10", "2026-11"]
    # 00:30 on 1 December in Berlin (23:30 UTC on 30 November) is December there, not in UTC.
    late = datetime(2026, 11, 30, 23, 30, tzinfo=UTC)
    assert month_of(late, BERLIN) == "2026-12"
    assert month_of(late, UTC) == "2026-11"
    # Summer time: 00:30 on 1 April is 22:30 UTC; winter time: 1 November starts at 23:00 UTC.
    assert month_of(datetime(2026, 3, 31, 22, 30, tzinfo=UTC), BERLIN) == "2026-04"
    assert month_of(datetime(2026, 10, 31, 22, 59, tzinfo=UTC), BERLIN) == "2026-10"
    assert month_of(datetime(2026, 10, 31, 23, 0, tzinfo=UTC), BERLIN) == "2026-11"
    months = deletions_by_month(
        scheduled=[datetime(2026, 10, 2, tzinfo=UTC), datetime(2026, 10, 31, 23, 30, tzinfo=UTC)],
        restored=[datetime(2026, 10, 9, tzinfo=UTC)],
        completed=[NOW, datetime(2025, 1, 1, tzinfo=UTC)],  # the old one is outside
        now=NOW,
        months=3,
        zone=BERLIN,
    )
    assert [(m.month, m.scheduled, m.restored, m.completed) for m in months] == [
        ("2026-09", 0, 0, 0),
        ("2026-10", 1, 1, 0),
        ("2026-11", 1, 0, 1),
    ]


def test_an_unknown_zone_is_refused() -> None:
    assert report_zone("Europe/Berlin").key == "Europe/Berlin"
    for bad in ("Mars/Olympus_Mons", "Europe/../etc", ""):
        with pytest.raises(InvalidInput):
            report_zone(bad)


async def test_platform_deletions_for_operator_admins_only() -> None:
    async with harness() as h:
        lukas = await h.register("lukas", account_name="Lukas home")
        anna = await h.register("anna", account_name="Anna home")
        path = f"{API}/platform/deletions"
        assert (await h.client.get(path)).status_code == 401
        assert (await h.client.get(path, headers=lukas.headers)).status_code == 403
        await h.app.state.identity.platform.set_admin("lukas", SYSTEM)

        now = h.clock()
        async with h.store.transaction() as tx:
            for action in ("account.deletion_requested", "account.deletion_requested",
                           "account.restored"):  # fmt: skip
                await tx.audit.add(AuditEntry(at=now, action=action, actor_subject=None,
                                              actor_kind="user", account_id=uuid4()))  # fmt: skip
            await tx.audit.add(
                AuditEntry(at=now, action="account.deletion_requested", actor_subject=None,
                           actor_kind="user", outcome="denied")
            )  # fmt: skip
            done = AccountErasure(uuid4(), now - timedelta(days=1), ["fleet"])
            done.state, done.completed_at = ErasureState.ERASED, now
            await tx.erasures.add(done)
            await tx.erasures.add(AccountErasure(uuid4(), now, ["fleet"]))
            account = await tx.accounts.get(id=anna.account_id)
            assert account is not None
            account.status = AccountStatus.DELETING
            await tx.accounts.update(account)

        params: dict[str, str | int] = {"months": 2, "tz": "Europe/Berlin"}
        answer = await h.client.get(path, headers=lukas.headers, params=params)
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["time_zone"] == "Europe/Berlin"
        assert [m["month"] for m in body["months"]] == recent_months(now, 2, BERLIN)
        assert body["months"][-1] == {
            "month": month_of(now, BERLIN),
            "scheduled": 2,
            "restored": 1,
            "completed": 1,
        }
        assert (body["in_grace"], body["purging"]) == (1, 1)
        assert str(anna.account_id) not in answer.text
        default = await h.client.get(path, headers=lukas.headers)
        assert default.json()["time_zone"] == "Europe/Berlin"
        utc = await h.client.get(path, headers=lukas.headers, params={"tz": "UTC"})
        assert utc.json()["time_zone"] == "UTC"
        unknown = await h.client.get(path, headers=lukas.headers, params={"tz": "Mars/Base"})
        assert unknown.status_code == 422
        too_many = await h.client.get(path, headers=lukas.headers, params={"months": 37})
        assert too_many.status_code == 422

# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account deletions per month for the operator's payments dashboard (ADR 0083): the pure
month rule and ``GET /identity/platform/deletions`` (operator admins only, counts only)."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

from identity_support import API, harness

from identity.application.context import SYSTEM
from identity.domain.deletion_stats import deletions_by_month, month_of, recent_months
from identity.domain.erasure import AccountErasure, ErasureState
from identity.domain.models import AccountStatus, AuditEntry

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


def test_deletions_are_counted_per_utc_month() -> None:
    assert recent_months(NOW, 3) == ["2026-09", "2026-10", "2026-11"]
    assert month_of(datetime(2026, 12, 1, 0, 30, tzinfo=timezone(timedelta(hours=1)))) == "2026-11"
    months = deletions_by_month(
        scheduled=[datetime(2026, 10, 2, tzinfo=UTC), datetime(2026, 10, 30, tzinfo=UTC), NOW],
        restored=[datetime(2026, 10, 9, tzinfo=UTC)],
        completed=[NOW, datetime(2025, 1, 1, tzinfo=UTC)],  # the old one is outside
        now=NOW,
        months=3,
    )
    assert [(m.month, m.scheduled, m.restored, m.completed) for m in months] == [
        ("2026-09", 0, 0, 0),
        ("2026-10", 2, 1, 0),
        ("2026-11", 1, 0, 1),
    ]


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

        answer = await h.client.get(path, headers=lukas.headers, params={"months": 2})
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["time_zone"] == "UTC"
        assert [m["month"] for m in body["months"]] == recent_months(now, 2)
        assert body["months"][-1] == {
            "month": month_of(now),
            "scheduled": 2,
            "restored": 1,
            "completed": 1,
        }
        assert (body["in_grace"], body["purging"]) == (1, 1)
        assert str(anna.account_id) not in answer.text
        too_many = await h.client.get(path, headers=lukas.headers, params={"months": 37})
        assert too_many.status_code == 422

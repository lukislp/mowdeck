"""Domain rules: status transitions, plan file parsing, catalogue merge."""

from datetime import timedelta
from uuid import uuid4

import pytest
from entitlements_support import PLANS_FILE, T0, repository_plans
from mowdeck_contracts.v1 import FeatureDef, ModuleDescriptor, PlanSource, PlanStatus

from entitlements.application.catalogue import merge_features
from entitlements.domain.errors import InvalidInput
from entitlements.domain.models import AccountPlan, FeatureEntry, unlimited_plan
from entitlements.domain.planfile import parse_plan_file
from entitlements.domain.rules import (
    default_row,
    deny_all_plan,
    effective_status,
    initial_status,
    unknown_features,
)


def _row(status: PlanStatus, **values: object) -> AccountPlan:
    base: dict[str, object] = {
        "account_id": uuid4(),
        "plan_key": "home",
        "status": status,
        "since": T0,
        "source": PlanSource.MANUAL,
        "updated_at": T0,
    }
    return AccountPlan(**{**base, **values})  # type: ignore[arg-type]


def test_entitlements_trial_turns_expired_at_its_end() -> None:
    row = _row(PlanStatus.TRIAL, trial_until=T0 + timedelta(days=30))
    assert effective_status(row, T0 + timedelta(days=29, hours=23)) is PlanStatus.TRIAL
    assert effective_status(row, T0 + timedelta(days=30)) is PlanStatus.EXPIRED
    assert effective_status(_row(PlanStatus.TRIAL), T0 + timedelta(days=999)) is PlanStatus.TRIAL


def test_entitlements_grace_turns_expired_and_active_stays() -> None:
    grace = _row(PlanStatus.GRACE, grace_until=T0 + timedelta(days=14))
    assert effective_status(grace, T0) is PlanStatus.GRACE
    assert effective_status(grace, T0 + timedelta(days=14)) is PlanStatus.EXPIRED
    assert effective_status(_row(PlanStatus.ACTIVE), T0 + timedelta(days=999)) is PlanStatus.ACTIVE
    assert effective_status(_row(PlanStatus.EXPIRED), T0) is PlanStatus.EXPIRED


def test_entitlements_new_accounts_start_a_trial_when_the_plan_has_one() -> None:
    home = next(s for s in repository_plans() if s.key == "home").to_plan()
    status, until = initial_status(home, T0)
    assert (status, until) == (PlanStatus.TRIAL, T0 + timedelta(days=30))
    assert initial_status(unlimited_plan(), T0) == (PlanStatus.ACTIVE, None)
    row = default_row(uuid4(), home, T0)
    assert (row.source, row.status, row.plan_key) == (PlanSource.DEFAULT, PlanStatus.TRIAL, "home")


def test_entitlements_repository_plan_file_is_valid() -> None:
    specs = {s.key: s for s in parse_plan_file(PLANS_FILE.read_text(encoding="utf-8"))}
    assert set(specs) == {"unlimited", "home", "fleet"}
    home = specs["home"]
    assert home.is_default and home.trial_days == 30
    limits = home.limits
    assert (limits.max_mowers, limits.max_users, limits.max_sites) == (4, 5, 1)
    assert (limits.max_groups, limits.max_schedules, limits.history_days) == (2, 10, 90)
    assert specs["unlimited"].features == ["*"]
    assert specs["fleet"].limits.max_mowers is None
    assert "fleet.bulk_commands" in specs["fleet"].features
    assert "fleet.bulk_commands" not in home.features
    assert home.digest() == home.model_copy().digest()
    assert home.digest() != specs["fleet"].digest()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("plans: [", "not valid YAML"),
        ("plans:\n  - key: a\n    name: A\n  - key: a\n    name: B\n", "duplicate plan keys"),
        (
            "plans:\n  - {key: a, name: A, is_default: true}\n"
            "  - {key: b, name: B, is_default: true}\n",
            "at most one plan",
        ),
        ("plans:\n  - {key: Bad, name: A}\n", "plans.0.key"),
        ("plans:\n  - {key: a, name: A, features: [NotAKey]}\n", "features"),
        ("plans:\n  - {key: a, name: A, limits: {max_cats: 1}}\n", "limits"),
        ("plans:\n  - {key: a, name: A, colour: red}\n", "colour"),
    ],
)
def test_entitlements_plan_file_errors_are_readable(text: str, message: str) -> None:
    with pytest.raises(InvalidInput, match=message):
        parse_plan_file(text)


def test_entitlements_empty_plan_file_has_no_plans() -> None:
    assert parse_plan_file("") == []


def test_entitlements_unknown_features_and_deny_all() -> None:
    home = next(s for s in repository_plans() if s.key == "home").to_plan()
    assert unknown_features(home, {"view", "control", "emergency_stop", "schedules"}) == [
        "ai_recover",
        "camera",
        "health",
        "home_assistant",
        "manual_drive",
        "map",
        "notifications",
        "spot_mowing",
    ]
    assert unknown_features(unlimited_plan(), set()) == []
    closed = deny_all_plan("gold")
    assert closed.features == [] and closed.limits.max_mowers == 0


def test_entitlements_catalogue_merge_adds_updates_and_retires() -> None:
    first = ModuleDescriptor(
        name="realtime",
        version="1.0.0",
        features=[
            FeatureDef(key="manual_drive", description="Drive"),
            FeatureDef(key="old_thing", description="Old"),
        ],
    )
    merged = merge_features([], [first], T0)
    assert [e.key for e in merged] == ["manual_drive", "old_thing"]
    later = T0 + timedelta(hours=1)
    second = ModuleDescriptor(
        name="realtime",
        version="1.1.0",
        features=[FeatureDef(key="manual_drive", description="Manual driving")],
    )
    other = FeatureEntry("schedules", "Schedules", "scheduler", T0, T0)
    again = {e.key: e for e in merge_features([*merged, other], [second], later)}
    assert again["manual_drive"].description == "Manual driving"
    assert again["manual_drive"].first_seen_at == T0
    assert again["manual_drive"].last_seen_at == later
    assert again["old_thing"].retired is True
    assert again["schedules"].retired is False  # its module did not answer: untouched
    back = {e.key: e for e in merge_features(list(again.values()), [first], later)}
    assert back["old_thing"].retired is False

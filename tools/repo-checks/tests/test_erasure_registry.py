# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Every service that owns tables answers the account erasure (ADR 0082).

Deleting an account must reach every service that stores something of it, so a service with
tables that Identity does not ask would silently keep personal data. The test fails for:

* a **new** service with a database that is neither answering ``account.erase.<service>`` nor
  listed in :data:`PENDING`;
* a service in :data:`PENDING` that answers already (delete its entry: the list only shrinks);
* a service in :data:`PENDING` that has no tables (a stale entry);
* a contract list (``ERASURE_SERVICES``) that differs from the services with tables.

:data:`PENDING` is the work of the follow-up steps of ADR 0082 and must be empty when step 2c
has landed: 2b Notifications and Vision; 2c Billing. Fleet, Scheduler, Telemetry, Planner and
Entitlements answer since step 2a.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from mowdeck_contracts.v1 import ERASURE_SERVICES
from mowdeck_contracts.v1.subjects import ACCOUNT_ERASE_PREFIX

ORCHESTRATOR = "identity"
"""Identity owns the accounts and asks the others; it erases its own rows itself."""

PENDING: dict[str, str] = {
    "notifications": "2b",
    "vision": "2b",
    "billing": "2c",
}
"""Services that own tables and do not answer ``account.erase.<service>`` yet, with the step of
ADR 0082 that adds the handler. Shrinks to empty by step 2c."""


def _nats_auth(repo_root: Path) -> ModuleType:
    path = repo_root / "tools" / "repo-checks" / "scripts" / "nats_auth.py"
    spec = importlib.util.spec_from_file_location("nats_auth_for_erasure", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _services_with_tables(repo_root: Path) -> set[str]:
    """Services whose package has a ``adapters/db/tables.py`` (a database of their own)."""
    return {
        tables.parents[4].name
        for tables in (repo_root / "services").glob("*/src/*/adapters/db/tables.py")
    }


def _answering(repo_root: Path) -> set[str]:
    """Services that serve their ``account.erase.<service>`` subject (the NATS permission map
    is checked against the code by ``test_nats_auth``)."""
    users = _nats_auth(repo_root).USERS
    return {
        name for name, access in users.items() if f"{ACCOUNT_ERASE_PREFIX}.{name}" in access.serve
    }


def _unregistered(with_tables: set[str], answering: set[str], pending: set[str]) -> set[str]:
    """Services with tables that neither answer nor are pending (Identity asks, it is exempt)."""
    return with_tables - {ORCHESTRATOR} - answering - pending


@pytest.fixture(scope="module")
def with_tables(repo_root: Path) -> set[str]:
    return _services_with_tables(repo_root)


def test_the_scan_finds_the_services_with_a_database(with_tables: set[str]) -> None:
    assert {"identity", "fleet", "telemetry"} <= with_tables
    assert "assistant" not in with_tables  # it owns no data


def test_every_service_with_tables_answers_or_is_pending(
    repo_root: Path, with_tables: set[str]
) -> None:
    answering = _answering(repo_root)
    unregistered = _unregistered(with_tables, answering, set(PENDING))
    assert not unregistered, (
        f"{sorted(unregistered)} own tables but are neither answering account.erase.<service> "
        "nor listed in PENDING: add the handler (ADR 0082) or, for the follow-up steps only, "
        "the PENDING entry with its step"
    )


def test_pending_only_shrinks(repo_root: Path, with_tables: set[str]) -> None:
    answering = _answering(repo_root)
    done = sorted(set(PENDING) & answering)
    assert not done, f"{done} answer account.erase.<service> now: remove them from PENDING"
    stale = sorted(set(PENDING) - with_tables)
    assert not stale, f"{stale} own no tables: remove them from PENDING"


def test_the_contract_lists_exactly_the_services_with_tables(with_tables: set[str]) -> None:
    assert set(ERASURE_SERVICES) == with_tables - {ORCHESTRATOR}


def test_identity_may_ask_every_service(repo_root: Path) -> None:
    users = _nats_auth(repo_root).USERS
    assert f"{ACCOUNT_ERASE_PREFIX}.*" in users[ORCHESTRATOR].publish


def test_no_service_answers_an_erasure_it_is_not_registered_for(repo_root: Path) -> None:
    answering = _answering(repo_root)
    assert answering <= set(ERASURE_SERVICES)


def test_a_new_service_with_tables_fails_until_it_is_registered(with_tables: set[str]) -> None:
    """The guard is strict: a service nobody listed is reported, answering or pending is not."""
    assert _unregistered(with_tables | {"newsvc"}, set(), set(PENDING)) == {"newsvc"} | (
        with_tables - {ORCHESTRATOR} - set(PENDING)
    )
    assert _unregistered(with_tables | {"newsvc"}, {"newsvc"}, set(PENDING)) == (
        with_tables - {ORCHESTRATOR} - set(PENDING)
    )
    assert _unregistered(with_tables | {"newsvc"}, set(), set(PENDING) | {"newsvc"}) == (
        with_tables - {ORCHESTRATOR} - set(PENDING)
    )


def _erasure_default() -> bool:
    from identity.settings import Settings  # the workspace installs every service

    default = Settings.model_fields["identity_erasure_enabled"].default
    assert isinstance(default, bool)
    return default


def test_the_purge_is_off_exactly_while_services_are_pending() -> None:
    """The switch cannot be forgotten: ``MOWDECK_IDENTITY_ERASURE_ENABLED`` stays false by
    default until every service answers (step 2c empties PENDING), and then it must be true.

    A purge that runs while a service does not answer would lock an account for good without
    deleting it; a default that stays false afterwards would leave accounts undeleted."""
    default = _erasure_default()
    if PENDING:
        assert default is False, (
            f"{sorted(PENDING)} do not answer account.erase.<service> yet: the purge must stay "
            "off by default (identity_erasure_enabled)"
        )
    else:
        assert default is True, (
            "every service answers account.erase.<service>: switch the purge on by default "
            "(identity_erasure_enabled=True in identity.settings, .env.example and the values)"
        )


def test_the_deployment_files_carry_the_same_default(repo_root: Path) -> None:
    expected = "true" if _erasure_default() else "false"
    env = (repo_root / "infra/compose/.env.example").read_text("utf-8")
    assert f"IDENTITY_ERASURE_ENABLED={expected}\n" in env
    compose = (repo_root / "infra/compose/docker-compose.yml").read_text("utf-8")
    assert f"${{IDENTITY_ERASURE_ENABLED:-{expected}}}" in compose
    values = (repo_root / "infra/helm/mowdeck/values.yaml").read_text("utf-8")
    assert f"IDENTITY_ERASURE_ENABLED: {expected}\n" in values

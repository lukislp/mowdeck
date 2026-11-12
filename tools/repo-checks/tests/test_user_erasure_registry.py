# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Every service that stores something personal of a user forgets a deleted user (ADR 0082).

A member can delete themselves ("delete me") and an admin can delete a sub-user; Identity then
publishes ``identity.user.deleted``. The account goes on, so the account erasure never runs for
it: a service that keeps a user's own rows or a copy of their name must react to the event, or it
keeps personal data of somebody who is gone.

What is personal is read from each service's schema, with the kit's helpers:

* a table with a ``user_id`` column holds rows that are a user's own;
* a JSON column named ``owner`` or ``<verb>_by`` stores a ``PrincipalRef``, which carries the
  person's name.

A plain id (``created_by`` as a UUID) is not personal: it names nobody once Identity's user row
is gone.

A service **forgets** when it has the inbound adapter ``adapters/bus/user_erasure.py`` (the kit's
``user_deleted_handler`` over its ``UserErasure`` use case, which fails loudly while something
personal is left) and may consume the event. The test fails for:

* a service that stores something personal and neither forgets nor is listed in
  :data:`USER_PENDING`;
* a service in :data:`USER_PENDING` that forgets already (delete its entry: the list only
  shrinks), or that stores nothing personal (a stale entry);
* a service with the adapter that may not consume the event.

:data:`USER_PENDING` was the follow-up of step 3 for Notifications and Vision, which reacted to
the event with handlers of their own that predated the kit's use case. It is empty since that
follow-up and stays empty: a new service that stores something personal of a user forgets it
from its first day. The event is handled in one place per service, the kit's handler in that
adapter: no other module of a service's source names ``IDENTITY_USER_DELETED``.
"""

import importlib
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from mowdeck_contracts.v1 import IDENTITY_USER_DELETED
from mowdeck_contracts.v1.subjects import event_subject_for_type, subject_matches
from mowdeck_service_kit.db import principal_ref_columns, user_tables

ORCHESTRATOR = "identity"
"""Identity owns the users and deletes its own rows itself (``delete_user_rows``)."""

EVENT_SUBJECT = event_subject_for_type(IDENTITY_USER_DELETED)

USER_PENDING: dict[str, str] = {}
"""Services that store something personal of a user and do not forget it through the kit's use
case yet, with what is open. Empty since the follow-up of step 3 (Notifications and Vision), and
it stays empty."""


def _nats_auth(repo_root: Path) -> ModuleType:
    path = repo_root / "tools" / "repo-checks" / "scripts" / "nats_auth.py"
    spec = importlib.util.spec_from_file_location("nats_auth_for_user_erasure", path)
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


def _personal(service: str) -> list[str]:
    """What the schema of ``service`` holds of a user: its tables with a ``user_id`` column and
    its columns that store a principal reference."""
    metadata = importlib.import_module(f"{service}.adapters.db.tables").metadata
    rows = [table.name for table in user_tables(metadata)]
    names = [f"{column.table.name}.{column.name}" for column in principal_ref_columns(metadata)]
    return sorted(rows + names)


def _forgetting(repo_root: Path) -> set[str]:
    """Services with the inbound adapter of the user erasure."""
    return {
        adapter.parents[4].name
        for adapter in (repo_root / "services").glob("*/src/*/adapters/bus/user_erasure.py")
    }


def _unregistered(personal: set[str], forgetting: set[str], pending: set[str]) -> set[str]:
    """Services with personal data that neither forget nor are pending (Identity is exempt)."""
    return personal - {ORCHESTRATOR} - forgetting - pending


@pytest.fixture(scope="module")
def personal(repo_root: Path) -> dict[str, list[str]]:
    """Per service with tables: what its schema holds of a user (empty: nothing)."""
    return {service: _personal(service) for service in sorted(_services_with_tables(repo_root))}


def test_the_scan_finds_what_is_personal(personal: dict[str, list[str]]) -> None:
    assert personal["fleet"] == ["commands.issued_by", "drive_sessions.started_by"]
    assert personal["scheduler"] == ["schedule_runs.issued_by", "schedules.owner"]
    assert "preferences" in personal["notifications"]
    assert "notification_images" not in personal["notifications"]  # no user_id column
    assert personal["vision"] == [
        "proposals.approved_by",
        "proposals.decided_by",
        "proposals.requested_by",
        "provider_requests",
        "user_preferences",
        "user_providers",
    ]
    assert personal["planner"] == []  # created_by there is a plain id


def test_every_service_with_personal_data_forgets_or_is_pending(
    repo_root: Path, personal: dict[str, list[str]]
) -> None:
    holding = {service for service, found in personal.items() if found}
    unregistered = _unregistered(holding, _forgetting(repo_root), set(USER_PENDING))
    assert not unregistered, (
        f"{ {s: personal[s] for s in sorted(unregistered)} } is personal data of a user: add "
        "the user erasure (mowdeck_service_kit.user_erasure, adapters/bus/user_erasure.py, "
        "ADR 0082) or, for the follow-up of step 2b only, a USER_PENDING entry"
    )


def test_user_pending_only_shrinks(repo_root: Path, personal: dict[str, list[str]]) -> None:
    done = sorted(set(USER_PENDING) & _forgetting(repo_root))
    assert not done, (
        f"{done} forget a deleted user through the kit now: remove them from USER_PENDING"
    )
    stale = sorted(service for service in USER_PENDING if not personal.get(service))
    assert not stale, f"{stale} store nothing personal of a user: remove them from USER_PENDING"


def test_a_forgetting_service_may_consume_the_event(repo_root: Path) -> None:
    users = _nats_auth(repo_root).USERS
    for service in sorted(_forgetting(repo_root)):
        allowed = any(subject_matches(pattern, EVENT_SUBJECT) for pattern in users[service].consume)
        assert allowed, (
            f"{service} has adapters/bus/user_erasure.py but may not consume {EVENT_SUBJECT}"
        )


def test_the_kits_handler_is_the_only_one_of_the_event(repo_root: Path) -> None:
    """One place per service: the event reaches a service through its user erasure adapter,
    never also through a handler of its own (as Notifications and Vision had before)."""
    services = repo_root / "services"
    others = sorted(
        source.relative_to(services).as_posix()
        for source in services.glob("*/src/**/*.py")
        if source.relative_to(services).parts[0] != ORCHESTRATOR
        and source.name != "user_erasure.py"
        and "IDENTITY_USER_DELETED" in source.read_text("utf-8")
    )
    assert not others, f"{others} handle identity.user.deleted outside adapters/bus/user_erasure.py"


def test_identity_publishes_the_event(repo_root: Path) -> None:
    users = _nats_auth(repo_root).USERS
    assert any(subject_matches(p, EVENT_SUBJECT) for p in users[ORCHESTRATOR].publish)


def test_a_new_service_with_personal_data_fails_until_it_is_registered() -> None:
    """The guard is strict: a service nobody listed is reported, forgetting or pending is not."""
    assert _unregistered({"newsvc", "fleet"}, {"fleet"}, set()) == {"newsvc"}
    assert _unregistered({"newsvc"}, {"newsvc"}, set()) == set()
    assert _unregistered({"newsvc"}, set(), {"newsvc"}) == set()
    assert _unregistered({ORCHESTRATOR}, set(), set()) == set()

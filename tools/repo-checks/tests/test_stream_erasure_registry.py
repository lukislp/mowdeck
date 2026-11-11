# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Every stream has an owner that removes an erased account's messages from it (ADR 0082).

The JetStream streams keep messages for their retention (up to 30 days), so a deleted account's
data would outlive its rows there. Two services remove it, each for what it owns:

* **Fleet** owns the mowers: the streams whose subjects carry the mower id
  (``MOWER_STREAMS``) are purged by subject filter when a mower goes;
* **Identity** owns accounts and users: the streams whose subjects name neither
  (:data:`BY_ENVELOPE`) are read and the account's or user's messages erased one by one.

The test fails for:

* a **new stream** that is neither a mower stream, nor in :data:`BY_ENVELOPE`, nor listed in
  :data:`PENDING` with its reason;
* a **new subject** somebody publishes into a mower stream that the owner's purge filters do
  not cover (a new snapshot kind, a new per-mower subject);
* an owner whose NATS permissions do not allow the removal;
* a stream in :data:`PENDING` that an owner removes from already (delete the entry: the list
  only shrinks);
* an **event type** of the contracts that Identity neither reads when it removes an account's
  events (``EVENT_FILTERS``) nor lists as carrying ids only (``OPAQUE_EVENTS``, with the reason);
* a **key-value bucket or object store** in the code: none exists today, and one that holds
  account data needs an owner and a purge like a stream does.
"""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest
from mowdeck_contracts.v1.subjects import EVENTS, MOWER_STREAMS, STREAMS, event_subject_for_type

from identity.domain.erasure import EVENT_FILTERS, OPAQUE_EVENTS

MOWER_OWNER = "fleet"
"""Fleet owns the mowers and purges the subjects that carry a mower id."""

BY_ENVELOPE: dict[str, str] = {"EVENTS": "identity"}
"""Streams whose subjects name no mower: the owner that erases an account's messages by what
their envelope says."""

PENDING: dict[str, str] = {}
"""Streams nobody removes an erased account's messages from yet, with the reason. Empty; a new
stream goes here only with a reason, and the entry is removed when an owner purges it."""

_BUCKETS = re.compile(r"\b(create_key_value|key_value|create_object_store|object_store)\s*\(")


def _nats_auth(repo_root: Path) -> ModuleType:
    path = repo_root / "tools" / "repo-checks" / "scripts" / "nats_auth.py"
    spec = importlib.util.spec_from_file_location("nats_auth_for_streams", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen(repo_root: Path) -> ModuleType:
    return _nats_auth(repo_root)


def test_every_stream_has_an_owner_or_a_reason() -> None:
    mower = {spec.name for spec in MOWER_STREAMS}
    assert mower.isdisjoint(BY_ENVELOPE) and mower.isdisjoint(PENDING)
    assert set(BY_ENVELOPE).isdisjoint(PENDING)
    assert mower | set(BY_ENVELOPE) | set(PENDING) == {spec.name for spec in STREAMS}
    assert all(reason.strip() for reason in PENDING.values())


def test_every_subject_published_into_a_mower_stream_is_purged_by_fleet(gen: ModuleType) -> None:
    """The purge filters are per kind of subject. A subject a service starts to publish into one
    of these streams must name the mower and be covered by a filter, or it would stay."""
    purged = gen.USERS[MOWER_OWNER].purge
    mower = {spec.name for spec in MOWER_STREAMS}
    assert {gen.stream_of(subject) for subject in purged} == mower
    uncovered = [
        f"{user}: {subject}"
        for user, access in gen.USERS.items()
        for subject in access.publish
        if gen.stream_of(subject) in mower
        and not any(gen.covers(pattern, subject) for pattern in purged)
    ]
    assert uncovered == []
    # Every filter names one mower: the token after "<kind>.mower" (or "<kind>.result.mower").
    for pattern in purged:
        tokens = pattern.split(".")
        assert tokens[tokens.index("mower") + 1] == "*", pattern


def test_the_owner_of_an_envelope_stream_may_erase_all_of_its_subjects(gen: ModuleType) -> None:
    specs = {spec.name: spec for spec in STREAMS}
    for stream, owner in BY_ENVELOPE.items():
        erased = gen.USERS[owner].erase
        for subject in specs[stream].subjects:
            assert any(gen.covers(pattern, subject) for pattern in erased), (stream, subject)


def test_the_owners_have_the_permissions_and_pending_streams_have_none(gen: ModuleType) -> None:
    config = gen.render()
    for spec in MOWER_STREAMS:
        assert f'"$JS.API.STREAM.PURGE.{spec.name}"' in config
    for stream in BY_ENVELOPE:
        assert f'"$JS.API.STREAM.MSG.DELETE.{stream}"' in config
    for stream in PENDING:  # an entry whose stream is purged already is stale
        assert f"STREAM.PURGE.{stream}" not in config
        assert f"STREAM.MSG.DELETE.{stream}" not in config


def test_no_bucket_or_object_store_exists_without_an_owner(repo_root: Path) -> None:
    """MowDeck keeps nothing in a JetStream key-value bucket or object store. Whoever adds one
    that holds data of an account, a user or a mower gives it an owner that purges it and
    extends this registry."""
    found = [
        str(path.relative_to(repo_root))
        for base in ("services", "libs")
        for path in (repo_root / base).glob("*/src/**/*.py")
        if _BUCKETS.search(path.read_text(encoding="utf-8"))
    ]
    assert found == []


def test_every_event_type_is_read_or_known_to_carry_ids_only(gen: ModuleType) -> None:
    """Identity does not read the whole ``EVENTS`` stream for an erased account: it skips the
    events that carry ids only (they are most of the stream). A new event type must be put on
    one of the two lists, so that one with a name or a text in it cannot be skipped unnoticed."""
    read, skipped = EVENT_FILTERS, tuple(OPAQUE_EVENTS)
    assert all(EVENTS.captures(s.replace("*", "x").replace(">", "x")) for s in (*read, *skipped))
    published = [
        pattern
        for access in gen.USERS.values()
        for pattern in access.publish
        if pattern.startswith("event.")
    ]
    unassigned = []
    for event_type in sorted(gen.event_types()):
        subject = event_subject_for_type(event_type)
        if not any(gen.covers(pattern, subject) for pattern in published):
            continue  # not an event on EVENTS (camera relay events, core subjects)
        is_read = any(gen.covers(pattern, subject) for pattern in read)
        is_skipped = any(
            gen.covers(pattern, subject) or gen.covers(pattern, f"{subject}.*")
            for pattern in skipped
        )
        if is_read == is_skipped:
            unassigned.append(event_type)
    assert unassigned == []
    # Whatever is read, Identity may read: its ``erase`` permission covers every filter.
    erased = gen.USERS[BY_ENVELOPE["EVENTS"]].erase
    assert all(any(gen.covers(pattern, subject) for pattern in erased) for subject in read)

"""PERMISSION_RELATIONS must match the packaged model exactly."""

from typing import Any

import pytest
from mowdeck_authz import GRANT_RELATIONS, PERMISSION_RELATIONS, Relation, grant_relation
from mowdeck_authz.model import load_model, models_equal, normalize_model

MODEL = load_model()
TYPES: dict[str, dict[str, Any]] = {t["type"]: t for t in MODEL["type_definitions"]}
LEVELS = ("account", "site", "group", "mower")


def _relations(type_name: str) -> dict[str, Any]:
    return TYPES[type_name].get("relations") or {}


def _direct_types(type_name: str, relation: str) -> set[str]:
    meta = TYPES[type_name]["metadata"]["relations"][relation]
    return {r["type"] for r in meta.get("directly_related_user_types", [])}


@pytest.mark.parametrize("key", sorted(PERMISSION_RELATIONS))
def test_permission_matches_model(key: str) -> None:
    entry = PERMISSION_RELATIONS[key]
    for level in LEVELS:
        has_grant = entry.grant in _relations(level)
        assert has_grant == (level in entry.levels), f"{key}: {entry.grant} on {level}"
        if has_grant:
            assert entry.computed in _relations(level), f"{key}: {entry.computed} on {level}"
            assert _direct_types(level, entry.grant) == set(entry.subject_types)


def test_every_grant_relation_of_the_model_is_mapped() -> None:
    in_model = {r for level in LEVELS for r in _relations(level) if r.startswith("grant_")}
    assert in_model == GRANT_RELATIONS


def test_every_relation_enum_member_exists_somewhere() -> None:
    defined = {r for t in TYPES for r in _relations(t)}
    assert {str(r) for r in Relation} <= defined
    assert not any(str(r).startswith("grant_") for r in Relation)


def test_grant_relation_validates_level_and_subject() -> None:
    assert grant_relation("mower.control", "group") == "grant_mower_control"
    assert grant_relation("mower.view", "mower", "api_token") == "grant_mower_view"
    with pytest.raises(ValueError, match="cannot be granted on mower"):
        grant_relation("users.manage", "mower")
    with pytest.raises(ValueError, match="cannot be granted to api_token"):
        grant_relation("users.manage", "account", "api_token")
    with pytest.raises(ValueError, match="on mower"):
        grant_relation("fleet.bulk_command", "mower")
    with pytest.raises(KeyError, match="unknown permission key"):
        grant_relation("mower.fly", "mower")


def test_model_normalisation_ignores_ids_and_empty_fields() -> None:
    noisy = {
        "id": "01ABC",
        "schema_version": "1.1",
        "type_definitions": [
            {"type": "user", "relations": {}, "metadata": None},
            {"type": "doc", "relations": {"r": {"this": {}}}, "metadata": {"module": ""}},
        ],
        "conditions": {},
    }
    clean = {
        "schema_version": "1.1",
        "type_definitions": [{"type": "user"}, {"type": "doc", "relations": {"r": {"this": {}}}}],
    }
    assert models_equal(noisy, clean)
    assert normalize_model(noisy) == normalize_model(clean)
    changed = {**clean, "type_definitions": [{"type": "user"}]}
    assert not models_equal(noisy, changed)

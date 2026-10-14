"""Contract: the in-memory fake answers every assertion of infra/openfga/model.fga.yaml.

The same YAML is run by ``fga model test`` in CI (``openfga-model`` job) and through
``OpenFgaAuthorizer`` against a real OpenFGA in ``test_authz_integration.py``. If the model
changes and the fake does not follow, this test fails.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from mowdeck_authz import ObjectRef, RelationshipTuple, Subject, TupleCondition, load_model
from mowdeck_authz.testing import InMemoryAuthorizer


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "infra" / "openfga" / "model.fga.yaml").is_file():
            return parent
    msg = "infra/openfga not found"
    raise RuntimeError(msg)


OPENFGA_DIR = _repo_root() / "infra" / "openfga"
SPEC: dict[str, Any] = yaml.safe_load((OPENFGA_DIR / "model.fga.yaml").read_text("utf-8"))


def yaml_tuple(raw: dict[str, Any]) -> RelationshipTuple:
    """Convert a tuple of the FGA test file."""
    condition = None
    if raw.get("condition"):
        condition = TupleCondition.of(raw["condition"]["name"], raw["condition"].get("context"))
    return RelationshipTuple.parse(raw["user"], raw["relation"], raw["object"], condition)


def spec_tuples(test: dict[str, Any]) -> list[RelationshipTuple]:
    """Global tuples plus the tuples of one test."""
    return [yaml_tuple(t) for t in [*SPEC.get("tuples", []), *test.get("tuples", [])]]


def _count(kind: str) -> int:
    if kind == "check":
        return sum(len(c["assertions"]) for test in SPEC["tests"] for c in test.get("check", []))
    return sum(len(c["assertions"]) for test in SPEC["tests"] for c in test.get("list_objects", []))


def test_spec_size_matches_the_cli_run() -> None:
    # fga model test reports 8 tests, 58 checks and 2 list_objects for this file.
    assert len(SPEC["tests"]) == 8
    assert _count("check") == 58
    assert _count("list_objects") == 2


@pytest.mark.parametrize("test", SPEC["tests"], ids=[t["name"] for t in SPEC["tests"]])
async def test_fake_matches_model_spec(test: dict[str, Any]) -> None:
    fake = InMemoryAuthorizer(spec_tuples(test))
    failures: list[str] = []
    for case in test.get("check", []):
        subject = Subject.parse(case["user"])
        obj = ObjectRef.parse(case["object"])
        for relation, expected in case["assertions"].items():
            actual = await fake.check(subject, relation, obj, context=case.get("context"))
            if actual != expected:
                failures.append(f"{subject} {relation} {obj}: {actual} != {expected}")
    for case in test.get("list_objects", []):
        subject = Subject.parse(case["user"])
        for relation, expected in case["assertions"].items():
            listed = await fake.list_objects(
                subject, relation, case["type"], context=case.get("context")
            )
            if sorted(listed) != sorted(expected):
                failures.append(f"list {subject} {relation} {case['type']}: {listed}")
    assert not failures, "\n".join(failures)


def test_packaged_json_has_the_types_and_relations_of_the_dsl() -> None:
    """Cheap staleness guard without Docker (CI also regenerates model.json and diffs it)."""
    dsl = (OPENFGA_DIR / "model.fga").read_text("utf-8")
    from_dsl: dict[str, set[str]] = {}
    current: str | None = None
    for line in dsl.splitlines():
        if match := re.match(r"^type (\w+)", line):
            current = match.group(1)
            from_dsl[current] = set()
        elif (match := re.match(r"^\s+define (\w+):", line)) and current is not None:
            from_dsl[current].add(match.group(1))
    model = load_model()
    from_json = {
        t["type"]: set((t.get("relations") or {}).keys()) for t in model["type_definitions"]
    }
    assert from_json == from_dsl
    assert set(model.get("conditions", {})) == set(re.findall(r"^condition (\w+)\(", dsl, re.M))

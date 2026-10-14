"""The model evaluator behind InMemoryAuthorizer, on a small model with every construct."""

from typing import Any

import pytest
from mowdeck_authz import ObjectRef, RelationshipTuple, Subject, TupleCondition
from mowdeck_authz.evaluator import ModelEvaluator, TupleIndex


def this() -> dict[str, Any]:
    return {"this": {}}


def computed(relation: str) -> dict[str, Any]:
    return {"computedUserset": {"relation": relation}}


def ttu(tupleset: str, relation: str) -> dict[str, Any]:
    return {
        "tupleToUserset": {
            "tupleset": {"relation": tupleset},
            "computedUserset": {"relation": relation},
        }
    }


MODEL: dict[str, Any] = {
    "schema_version": "1.1",
    "type_definitions": [
        {"type": "user"},
        {
            "type": "team",
            "relations": {"member": this()},
            "metadata": {
                "relations": {"member": {"directly_related_user_types": [{"type": "user"}]}}
            },
        },
        {
            "type": "doc",
            "relations": {
                "parent": this(),
                "viewer": this(),
                "blocked": this(),
                "limited": this(),
                "loop": computed("loop2"),
                "loop2": computed("loop"),
                "can_read": {
                    "difference": {"base": computed("viewer"), "subtract": computed("blocked")}
                },
                "inherited": ttu("parent", "viewer"),
                "via_parent_missing": ttu("parent", "nothing_there"),
            },
            "metadata": {
                "relations": {
                    "parent": {"directly_related_user_types": [{"type": "doc"}]},
                    "viewer": {
                        "directly_related_user_types": [
                            {"type": "user"},
                            {"type": "user", "wildcard": {}},
                            {"type": "team", "relation": "member"},
                        ]
                    },
                    "blocked": {"directly_related_user_types": [{"type": "user"}]},
                    "limited": {
                        "directly_related_user_types": [
                            {"type": "user", "condition": "small"},
                            {"type": "user", "condition": "named"},
                        ]
                    },
                }
            },
        },
    ],
    "conditions": {
        "small": {
            "name": "small",
            "expression": "size <= limit",
            "parameters": {
                "size": {"type_name": "TYPE_NAME_INT"},
                "limit": {"type_name": "TYPE_NAME_DOUBLE"},
            },
        },
        "named": {
            "name": "named",
            "expression": "who == expected",
            "parameters": {
                "who": {"type_name": "TYPE_NAME_STRING"},
                "expected": {"type_name": "TYPE_NAME_STRING"},
            },
        },
    },
}

ALICE, BOB = Subject("user", "alice"), Subject("user", "bob")
D1, D2 = ObjectRef("doc", "d1"), ObjectRef("doc", "d2")


def t(
    subject: Subject, relation: str, obj: ObjectRef, cond: TupleCondition | None = None
) -> RelationshipTuple:
    return RelationshipTuple(subject, relation, obj, cond)


def test_wildcards_usersets_difference_and_parents() -> None:
    evaluator = ModelEvaluator(MODEL)
    team = ObjectRef("team", "t1")
    index = TupleIndex(
        [
            t(Subject("team", "t1", "member"), "viewer", D1),
            t(ALICE, "member", team),
            t(Subject("user", "*"), "viewer", D2),
            t(BOB, "blocked", D2),
            t(D1.as_subject(), "parent", D2),
        ]
    )
    assert evaluator.check(index, ALICE, "viewer", D1)  # through the team userset
    assert not evaluator.check(index, BOB, "viewer", D1)
    assert evaluator.check(index, Subject("team", "t1", "member"), "viewer", D1)
    assert evaluator.check(index, BOB, "viewer", D2)  # wildcard
    assert not evaluator.check(index, BOB, "can_read", D2)  # but blocked
    assert evaluator.check(index, ALICE, "can_read", D2)
    assert evaluator.check(index, ALICE, "inherited", D2)  # viewer of the parent d1
    assert not evaluator.check(index, ALICE, "via_parent_missing", D2)
    assert not evaluator.check(index, ALICE, "loop", D1)  # cycles end at the depth limit
    assert evaluator.list_objects(index, ALICE, "viewer", "doc") == ["doc:d1", "doc:d2"]
    assert evaluator.types() == ["doc", "team", "user"]
    assert "can_read" in evaluator.relations("doc")
    with pytest.raises(ValueError, match="not defined"):
        evaluator.list_objects(index, ALICE, "owner", "doc")


def test_conditions_of_several_types() -> None:
    evaluator = ModelEvaluator(MODEL)
    index = TupleIndex(
        [
            t(ALICE, "limited", D1, TupleCondition.of("small", {"limit": 2.5})),
            t(BOB, "limited", D1, TupleCondition.of("named", {"expected": "bob"})),
        ]
    )
    assert evaluator.check(index, ALICE, "limited", D1, {"size": 2})
    assert not evaluator.check(index, ALICE, "limited", D1, {"size": 3})
    assert not evaluator.check(index, ALICE, "limited", D1, {})  # missing parameter
    assert not evaluator.check(index, ALICE, "limited", D1, {"size": "many"})  # bad value
    assert evaluator.check(index, BOB, "limited", D1, {"who": "bob"})
    # The tuple context wins over the request context for the same key.
    assert not evaluator.check(index, BOB, "limited", D1, {"who": "eve", "expected": "eve"})
    unknown = TupleIndex([t(ALICE, "limited", D1, TupleCondition.of("gone"))])
    assert not evaluator.check(unknown, ALICE, "limited", D1, {"size": 1})


def test_tuple_validation() -> None:
    evaluator = ModelEvaluator(MODEL)
    evaluator.validate_tuple(t(Subject("user", "*"), "viewer", D1))
    evaluator.validate_tuple(t(Subject("team", "t1", "member"), "viewer", D1))
    with pytest.raises(ValueError, match="type restrictions"):
        evaluator.validate_tuple(t(Subject("user", "*"), "blocked", D1))
    with pytest.raises(ValueError, match="type restrictions"):
        evaluator.validate_tuple(t(Subject("team", "t1"), "viewer", D1))
    with pytest.raises(ValueError, match="unknown condition"):
        evaluator.validate_tuple(t(ALICE, "limited", D1, TupleCondition.of("gone")))
    with pytest.raises(ValueError, match="not directly assignable"):
        evaluator.validate_tuple(t(ALICE, "can_read", D1))


def test_tuple_index() -> None:
    item = t(ALICE, "viewer", D1)
    index = TupleIndex([item])
    assert not index.add(item)
    assert item in index
    assert "not a tuple" not in index
    assert len(index) == 1
    assert index.remove(item)
    assert not index.remove(item)
    assert index.on(D1, "viewer") == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema_version": "1.2"}, "schema version"),
        (
            {"conditions": {"c": {"expression": "a < b || c", "parameters": {}}}},
            "unsupported expression",
        ),
        (
            {
                "conditions": {
                    "c": {
                        "expression": "a < b",
                        "parameters": {"a": {"type_name": "TYPE_NAME_DURATION"}},
                    }
                }
            },
            "unsupported parameter",
        ),
        (
            {"type_definitions": [{"type": "x", "relations": {"r": {"weird": {}}}}]},
            "unsupported rewrite",
        ),
    ],
)
def test_unsupported_models_are_rejected(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ModelEvaluator({**MODEL, **change})

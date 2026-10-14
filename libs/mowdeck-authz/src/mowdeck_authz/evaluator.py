"""A small, exact evaluator of OpenFGA models (schema 1.1) for tests.

It reads the JSON form of the model (the same one the bootstrap writes to OpenFGA), so the fake
authorizer cannot drift from the real model. Supported: direct relations (``this``) with type
restrictions, usersets and wildcards; ``computedUserset``; ``tupleToUserset``; ``union``,
``intersection`` and ``difference``; conditions whose expression is one comparison of two
parameters (``current_time < expires_at``). A model using anything else is rejected when the
evaluator is built, never silently mis-evaluated.

Like OpenFGA, the tuple context of a condition wins over the request context for the same key,
and a condition whose parameters are missing does not grant access.
"""

import operator
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any

from mowdeck_authz.refs import ObjectRef, Subject
from mowdeck_authz.tuples import RelationshipTuple

_MAX_DEPTH = 25
_COMPARISON = re.compile(r"^\s*([A-Za-z_]\w*)\s*(<=|>=|==|!=|<|>)\s*([A-Za-z_]\w*)\s*$")
_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}
_REWRITES = frozenset({"this", "computedUserset", "tupleToUserset", "union", "intersection"})


def _parse_timestamp(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


_CONVERTERS: dict[str, Callable[[Any], Any]] = {
    "TYPE_NAME_TIMESTAMP": _parse_timestamp,
    "TYPE_NAME_STRING": str,
    "TYPE_NAME_INT": int,
    "TYPE_NAME_UINT": int,
    "TYPE_NAME_DOUBLE": float,
    "TYPE_NAME_BOOL": bool,
}


class _Condition:
    def __init__(self, name: str, definition: Mapping[str, Any]) -> None:
        match = _COMPARISON.match(definition.get("expression", ""))
        if match is None:
            msg = f"condition {name}: unsupported expression {definition.get('expression')!r}"
            raise ValueError(msg)
        self.left, op, self.right = match.groups()
        self.op = _OPERATORS[op]
        params = definition.get("parameters", {})
        self.converters: dict[str, Callable[[Any], Any]] = {}
        for param in (self.left, self.right):
            type_name = params.get(param, {}).get("type_name")
            if type_name not in _CONVERTERS:
                msg = f"condition {name}: unsupported parameter {param!r} ({type_name})"
                raise ValueError(msg)
            self.converters[param] = _CONVERTERS[type_name]

    def evaluate(self, context: Mapping[str, Any]) -> bool:
        if self.left not in context or self.right not in context:
            return False
        try:
            left = self.converters[self.left](context[self.left])
            right = self.converters[self.right](context[self.right])
            return bool(self.op(left, right))
        except (TypeError, ValueError):
            return False


class TupleIndex:
    """Tuples indexed by ``(object, relation)``, keyed by ``(subject, relation, object)``."""

    def __init__(self, tuples: Iterable[RelationshipTuple] = ()) -> None:
        self._by_key: dict[tuple[str, str, str], RelationshipTuple] = {}
        self._by_object: defaultdict[tuple[str, str], dict[str, RelationshipTuple]] = defaultdict(
            dict
        )
        for item in tuples:
            self.add(item)

    def __len__(self) -> int:
        return len(self._by_key)

    def __iter__(self) -> Any:
        return iter(list(self._by_key.values()))

    def __contains__(self, item: object) -> bool:
        return isinstance(item, RelationshipTuple) and item.key() in self._by_key

    def add(self, item: RelationshipTuple) -> bool:
        """Add a tuple; False if a tuple with the same key exists already."""
        if item.key() in self._by_key:
            return False
        self._by_key[item.key()] = item
        self._by_object[(str(item.object), item.relation)][str(item.subject)] = item
        return True

    def remove(self, item: RelationshipTuple) -> bool:
        """Remove the tuple with the same key; False if there was none."""
        if self._by_key.pop(item.key(), None) is None:
            return False
        self._by_object[(str(item.object), item.relation)].pop(str(item.subject), None)
        return True

    def on(self, obj: ObjectRef, relation: str) -> list[RelationshipTuple]:
        """Tuples ``obj#relation@...``."""
        return list(self._by_object.get((str(obj), relation), {}).values())

    def objects_of_type(self, object_type: str) -> list[ObjectRef]:
        """Every object of ``object_type`` that appears on the object side of a tuple."""
        found = {item.object for item in self._by_key.values() if item.object.type == object_type}
        return sorted(found, key=str)


class ModelEvaluator:
    """Evaluates checks for one model over a :class:`TupleIndex`."""

    def __init__(self, model: Mapping[str, Any]) -> None:
        if model.get("schema_version") != "1.1":
            msg = f"unsupported schema version {model.get('schema_version')!r}"
            raise ValueError(msg)
        self._types: dict[str, Mapping[str, Any]] = {
            definition["type"]: definition for definition in model["type_definitions"]
        }
        self._conditions = {
            name: _Condition(name, definition)
            for name, definition in (model.get("conditions") or {}).items()
        }
        for type_name, definition in self._types.items():
            for relation, rewrite in (definition.get("relations") or {}).items():
                self._validate_rewrite(f"{type_name}#{relation}", rewrite)

    def _validate_rewrite(self, where: str, node: Mapping[str, Any]) -> None:
        kinds = [key for key in node if key in _REWRITES or key == "difference"]
        if len(kinds) != 1:
            msg = f"{where}: unsupported rewrite {sorted(node)}"
            raise ValueError(msg)
        kind = kinds[0]
        if kind in ("union", "intersection"):
            for child in node[kind]["child"]:
                self._validate_rewrite(where, child)
        elif kind == "difference":
            self._validate_rewrite(where, node[kind]["base"])
            self._validate_rewrite(where, node[kind]["subtract"])

    def has_relation(self, object_type: str, relation: str) -> bool:
        """True if ``object_type`` defines ``relation``."""
        return relation in (self._types.get(object_type, {}).get("relations") or {})

    def relations(self, object_type: str) -> list[str]:
        """Relation names of ``object_type``."""
        return sorted(self._types.get(object_type, {}).get("relations") or {})

    def types(self) -> list[str]:
        """Type names of the model."""
        return sorted(self._types)

    def validate_tuple(self, item: RelationshipTuple) -> None:
        """Raise ``ValueError`` if OpenFGA would reject writing ``item``.

        Checks that the relation exists and is directly assignable, and that the subject type,
        userset relation, wildcard and condition match one of its type restrictions.
        """
        definition = self._types.get(item.object.type)
        if definition is None:
            msg = f"{item}: unknown type {item.object.type!r}"
            raise ValueError(msg)
        metadata = (definition.get("metadata") or {}).get("relations") or {}
        allowed = (metadata.get(item.relation) or {}).get("directly_related_user_types") or []
        if not self.has_relation(item.object.type, item.relation) or not allowed:
            msg = f"{item}: {item.object.type}#{item.relation} is not directly assignable"
            raise ValueError(msg)
        condition = item.condition.name if item.condition else None
        if condition is not None and condition not in self._conditions:
            msg = f"{item}: unknown condition {condition!r}"
            raise ValueError(msg)
        subject = item.subject
        for restriction in allowed:
            if restriction.get("type") != subject.type:
                continue
            if subject.id == "*" and "wildcard" not in restriction:
                continue
            if subject.id != "*" and "wildcard" in restriction:
                continue
            if restriction.get("relation") != subject.relation:
                continue
            if restriction.get("condition") != condition:
                continue
            return
        msg = f"{item}: subject not allowed by the type restrictions of {item.relation}"
        raise ValueError(msg)

    def check(
        self,
        tuples: TupleIndex,
        subject: Subject,
        relation: str,
        obj: ObjectRef,
        context: Mapping[str, Any] | None = None,
    ) -> bool:
        """True if ``subject`` has ``relation`` on ``obj``.

        Raises ``ValueError`` for an unknown type or relation (OpenFGA answers those with a
        validation error, which ``OpenFgaAuthorizer`` turns into a deny).
        """
        if not self.has_relation(obj.type, relation):
            msg = f"{obj.type}#{relation} is not defined in the model"
            raise ValueError(msg)
        return self._check(tuples, subject, relation, obj, dict(context or {}), 0)

    def list_objects(
        self,
        tuples: TupleIndex,
        subject: Subject,
        relation: str,
        object_type: str,
        context: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """Objects of ``object_type`` with ``relation`` for ``subject``, sorted."""
        if not self.has_relation(object_type, relation):
            msg = f"{object_type}#{relation} is not defined in the model"
            raise ValueError(msg)
        return [
            str(obj)
            for obj in tuples.objects_of_type(object_type)
            if self._check(tuples, subject, relation, obj, dict(context or {}), 0)
        ]

    def _check(
        self,
        tuples: TupleIndex,
        subject: Subject,
        relation: str,
        obj: ObjectRef,
        context: dict[str, Any],
        depth: int,
    ) -> bool:
        if depth > _MAX_DEPTH:
            return False
        rewrite = (self._types.get(obj.type, {}).get("relations") or {}).get(relation)
        if rewrite is None:
            return False
        return self._rewrite(tuples, rewrite, subject, relation, obj, context, depth + 1)

    def _rewrite(
        self,
        tuples: TupleIndex,
        node: Mapping[str, Any],
        subject: Subject,
        relation: str,
        obj: ObjectRef,
        context: dict[str, Any],
        depth: int,
    ) -> bool:
        def recurse(child: Mapping[str, Any]) -> bool:
            return self._rewrite(tuples, child, subject, relation, obj, context, depth)

        if "this" in node:
            return self._direct(tuples, subject, relation, obj, context, depth)
        if "computedUserset" in node:
            target = node["computedUserset"]["relation"]
            return self._check(tuples, subject, target, obj, context, depth)
        if "tupleToUserset" in node:
            spec = node["tupleToUserset"]
            target = spec["computedUserset"]["relation"]
            for item in tuples.on(obj, spec["tupleset"]["relation"]):
                if item.subject.relation is not None or not self._condition_ok(item, context):
                    continue
                parent = item.subject.as_object()
                if self.has_relation(parent.type, target) and self._check(
                    tuples, subject, target, parent, context, depth
                ):
                    return True
            return False
        if "union" in node:
            return any(recurse(child) for child in node["union"]["child"])
        if "intersection" in node:
            return all(recurse(child) for child in node["intersection"]["child"])
        difference = node["difference"]
        return recurse(difference["base"]) and not recurse(difference["subtract"])

    def _direct(
        self,
        tuples: TupleIndex,
        subject: Subject,
        relation: str,
        obj: ObjectRef,
        context: dict[str, Any],
        depth: int,
    ) -> bool:
        for item in tuples.on(obj, relation):
            if not self._condition_ok(item, context):
                continue
            holder = item.subject
            if holder == subject:
                return True
            if (
                holder.relation is None
                and subject.relation is None
                and holder.id == "*"
                and holder.type == subject.type
            ):
                return True
            if holder.relation is not None and self._check(
                tuples, subject, holder.relation, holder.as_object(), context, depth
            ):
                return True
        return False

    def _condition_ok(self, item: RelationshipTuple, context: Mapping[str, Any]) -> bool:
        if item.condition is None:
            return True
        condition = self._conditions.get(item.condition.name)
        if condition is None:
            return False
        return condition.evaluate({**context, **item.condition.context_dict()})

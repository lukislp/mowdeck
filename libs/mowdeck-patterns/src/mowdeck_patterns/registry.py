"""Pattern registry (entry point group ``mowdeck.patterns``) and the chain combinator.

Another package adds a pattern without touching this one::

    [project.entry-points."mowdeck.patterns"]
    waves = "my_package.waves:WavesPattern"

The entry point name must equal the pattern's ``key``; the object is a class (or factory)
called without arguments. A failing entry point is logged and skipped.
"""

import logging
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from typing import Any

from mowdeck_patterns.base import MowPattern, build_plan
from mowdeck_patterns.geometry import ROUNDING_SLACK_M, Router, inset
from mowdeck_patterns.models import Area, MowerSpec, MowPlan, PatternError, Segment
from mowdeck_patterns.validation import validate

ENTRY_POINT_GROUP = "mowdeck.patterns"
MAX_CHAIN_STEPS = 8
"""Most patterns one chain may combine."""

EntryPointSource = Callable[[], Iterable[EntryPoint]]

_log = logging.getLogger("mowdeck_patterns.registry")


class UnknownPattern(PatternError):  # noqa: N818 - reads as the finding
    """No pattern with this key is registered."""


def _installed() -> Iterable[EntryPoint]:
    return entry_points(group=ENTRY_POINT_GROUP)


class PatternRegistry:
    """Maps pattern keys to pattern objects."""

    def __init__(self, entry_point_source: EntryPointSource = _installed) -> None:
        self._patterns: dict[str, MowPattern] = {}
        self._source = entry_point_source
        self._loaded = False

    def register(self, pattern: MowPattern, *, replace: bool = False) -> None:
        """Register ``pattern`` under its key (``ValueError`` if taken and not ``replace``)."""
        if not isinstance(pattern, MowPattern):
            msg = f"{pattern!r} does not implement MowPattern"
            raise TypeError(msg)
        existing = self._patterns.get(pattern.key)
        if existing is not None and existing is not pattern and not replace:
            msg = f"a pattern with key '{pattern.key}' is already registered"
            raise ValueError(msg)
        self._patterns[pattern.key] = pattern

    def load_entry_points(self) -> list[str]:
        """Register every pattern of the ``mowdeck.patterns`` entry points; returns the keys."""
        self._loaded = True
        loaded: list[str] = []
        for entry_point in self._source():
            try:
                factory = entry_point.load()
                pattern = factory()
            except Exception:
                _log.exception("cannot load pattern entry point %s", entry_point)
                continue
            if not isinstance(pattern, MowPattern):
                _log.warning("ignoring entry point %s: not a MowPattern", entry_point)
                continue
            if pattern.key != entry_point.name:
                _log.warning(
                    "ignoring entry point %s: its pattern key is %r", entry_point, pattern.key
                )
                continue
            if pattern.key in self._patterns:
                continue
            self._patterns[pattern.key] = pattern
            loaded.append(pattern.key)
        return loaded

    def _ensure(self) -> None:
        if not self._loaded:
            self.load_entry_points()

    def get(self, key: str) -> MowPattern:
        """The pattern ``key``; raises :class:`UnknownPattern`."""
        self._ensure()
        try:
            return self._patterns[key]
        except KeyError:
            msg = f"unknown pattern '{key}'"
            raise UnknownPattern(msg) from None

    def keys(self) -> list[str]:
        """Registered keys, sorted."""
        self._ensure()
        return sorted(self._patterns)

    def all(self) -> list[MowPattern]:
        """Registered patterns, sorted by key."""
        return [self._patterns[k] for k in self.keys()]


_default: PatternRegistry | None = None


def default_registry() -> PatternRegistry:
    """The process-wide registry of the installed patterns."""
    global _default
    if _default is None:
        _default = PatternRegistry()
    return _default


@dataclass(frozen=True, slots=True)
class ChainStep:
    """One pattern of a chain with its settings."""

    pattern: MowPattern
    settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Chain:
    """Patterns run one after the other, e.g. "2 border laps + diagonal + re-mow".

    Each step computes its own plan over the whole area; the steps are joined with blades-off
    transfers routed inside the area. The chain is itself pattern-like (``compute``).
    """

    steps: tuple[ChainStep, ...]

    @property
    def key(self) -> str:
        """``key1+key2+...``."""
        return "+".join(step.pattern.key for step in self.steps)

    def compute(self, area: Area, mower: MowerSpec) -> MowPlan:
        """The joined plan (not yet validated)."""
        if not self.steps:
            msg = "a chain needs at least one pattern"
            raise PatternError(msg)
        region = area.region()
        router = Router(inset(region, mower.safety_margin_m + ROUNDING_SLACK_M))
        segments: list[Segment] = []
        chain: list[str] = []
        warnings: list[str] = []
        for step in self.steps:
            plan = step.pattern.compute(area, mower, step.settings)
            chain.extend(plan.pattern_chain)
            warnings.extend(plan.warnings)
            part = list(plan.segments)
            if segments and part and math.dist(segments[-1].end, part[0].start) > 1e-6:
                path = router.route(segments[-1].end, part[0].start)
                segments.append(Segment(tuple(path), False, mower.transfer_speed_mps, "transfer"))
            segments.extend(part)
        return build_plan(
            segments, area, mower, tuple(chain), region=region, warnings=tuple(warnings)
        )


def chain(
    steps: Sequence[tuple[MowPattern | str, Mapping[str, Any]] | MowPattern | str],
    registry: PatternRegistry | None = None,
) -> Chain:
    """Build a :class:`Chain` from patterns or keys (with optional settings)."""
    if not steps:
        msg = "a chain needs at least one pattern"
        raise PatternError(msg)
    if len(steps) > MAX_CHAIN_STEPS:
        msg = f"a chain may combine at most {MAX_CHAIN_STEPS} patterns"
        raise PatternError(msg)
    active = registry or default_registry()
    built: list[ChainStep] = []
    for step in steps:
        target, settings = step if isinstance(step, tuple) else (step, {})
        pattern = active.get(target) if isinstance(target, str) else target
        built.append(ChainStep(pattern, dict(settings)))
    return Chain(tuple(built))


def plan(
    area: Area,
    mower: MowerSpec,
    steps: Sequence[tuple[MowPattern | str, Mapping[str, Any]] | MowPattern | str],
    registry: PatternRegistry | None = None,
) -> MowPlan:
    """Compute and validate: the one call the Planner makes. Raises ``PlanRejected``."""
    result = chain(steps, registry).compute(area, mower)
    return validate(result, area, mower)


__all__ = [
    "ENTRY_POINT_GROUP",
    "Chain",
    "ChainStep",
    "PatternRegistry",
    "UnknownPattern",
    "chain",
    "default_registry",
    "plan",
]

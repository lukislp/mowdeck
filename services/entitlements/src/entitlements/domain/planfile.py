"""The plan file (``infra/entitlements/plans.yaml``): plans as data, seeded at startup.

Format::

    plans:
      - key: home                 # lower-case, digits, '-' and '_'
        name: Home
        description: For one garden.
        is_default: true          # at most one plan
        trial_days: 30
        active: true
        limits:                   # omitted limits are unlimited
          max_mowers: 4
        features: [view, control, schedules]

Parsing is pure (text in, plans out); reading the file is the caller's business. Feature keys
that no module declares are kept (a plan file may lead a deployment); the service only warns.
"""

import hashlib
import json
from typing import Annotated, Any

import yaml
from mowdeck_contracts.v1 import ALL_FEATURES, LIMIT_KEYS, PlanLimits
from mowdeck_contracts.v1.module import FeatureKey
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from entitlements.domain.errors import InvalidInput
from entitlements.domain.models import Plan

PlanKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_\-]{0,63}$")]
"""A plan key such as ``home``."""

FeatureEntryKey = FeatureKey | Annotated[str, StringConstraints(pattern=r"^\*$")]


def check_limit_keys(value: Any) -> Any:
    """Reject unknown limit names: a typo must not silently mean "unlimited"."""
    if isinstance(value, dict):
        unknown = sorted(str(k) for k in value if k not in LIMIT_KEYS)
        if unknown:
            msg = f"unknown limits {', '.join(unknown)} (known: {', '.join(LIMIT_KEYS)})"
            raise ValueError(msg)
    return value


class PlanSpec(BaseModel):
    """One plan of the file (and the body of ``PUT /plans/{key}`` without ``key``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: PlanKey
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    limits: PlanLimits = Field(default_factory=PlanLimits)
    features: list[FeatureEntryKey] = Field(default_factory=list, max_length=500)
    trial_days: int = Field(default=0, ge=0, le=365)
    is_default: bool = False
    active: bool = True

    _limit_keys = field_validator("limits", mode="before")(check_limit_keys)

    def digest(self) -> str:
        """SHA-256 of the canonical JSON form (detects a changed file entry)."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_plan(self, *, file_digest: str | None = None) -> Plan:
        """The domain plan."""
        return Plan(
            key=self.key,
            name=self.name,
            description=self.description,
            limits=self.limits,
            features=sorted(set(self.features), key=lambda f: (f != ALL_FEATURES, f)),
            trial_days=self.trial_days,
            is_default=self.is_default,
            active=self.active,
            file_digest=file_digest,
        )


class PlanFile(BaseModel):
    """The whole file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plans: list[PlanSpec] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def _unique(self) -> "PlanFile":
        keys = [p.key for p in self.plans]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        if duplicates:
            msg = f"duplicate plan keys: {', '.join(duplicates)}"
            raise ValueError(msg)
        if sum(1 for p in self.plans if p.is_default) > 1:
            msg = "at most one plan may set is_default"
            raise ValueError(msg)
        return self


def parse_plan_file(text: str) -> list[PlanSpec]:
    """Parse the YAML text (``InvalidInput`` with a readable message when it is wrong)."""
    try:
        raw: Any = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        msg = f"the plan file is not valid YAML: {exc}"
        raise InvalidInput(msg) from exc
    try:
        return list(PlanFile.model_validate(raw).plans)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}" for error in exc.errors()
        )
        msg = f"the plan file is invalid: {problems}"
        raise InvalidInput(msg) from exc

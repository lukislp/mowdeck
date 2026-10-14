"""Self-describing modules.

Every service serves a :class:`ModuleDescriptor` at :data:`MODULE_DESCRIPTOR_PATH`. Identity,
Entitlements and the web app read these descriptors at startup, so the permissions, plan
features, events and UI areas of a new service appear without changes elsewhere.
"""

from collections import Counter
from collections.abc import Iterable
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from mowdeck_contracts.v1._base import ContractModel, DottedKey

MODULE_DESCRIPTOR_PATH = "/.well-known/mowdeck-module"
"""HTTP path at which every service serves its descriptor."""

FeatureKey = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$", max_length=128)
]
SemVer = Annotated[
    str,
    StringConstraints(pattern=r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-+]+)?$", max_length=64),
]


class PermissionDef(ContractModel):
    """A permission a module defines, e.g. ``mower.control``.

    Keys that have a relation in the authorization model are checked per object in OpenFGA.
    Other keys are coarse, account-wide permissions carried in the access token (``perms``) and
    checked with ``require_permission``. ``implied_by`` lets such a key follow an existing one:
    whoever holds every listed key on the whole account also holds this key.
    """

    key: DottedKey
    description: str = Field(min_length=1, max_length=500)
    implied_by: list[DottedKey] = Field(
        default_factory=list,
        description="Keys that imply this one when held on the whole account "
        "(e.g. `device_gateway.view` implied by `mower.view`).",
    )


class FeatureDef(ContractModel):
    """A plan feature a module offers, e.g. ``reports``."""

    key: FeatureKey
    description: str = Field(min_length=1, max_length=500)


class EventDef(ContractModel):
    """An event type a module publishes."""

    type: DottedKey
    version: int = Field(default=1, ge=1)
    schema_ref: str = Field(
        min_length=1, description="Reference to the payload JSON Schema (URI or package path)."
    )


class UiAreaDef(ContractModel):
    """An area the module occupies in the web app shell."""

    route: Annotated[str, StringConstraints(pattern=r"^/[A-Za-z0-9/_\-:\[\]]*$", max_length=200)]
    menu_label: str = Field(min_length=1, max_length=100)
    required_permission: DottedKey
    labels: dict[
        Annotated[str, StringConstraints(pattern=r"^[a-z]{2}(-[A-Z]{2})?$")],
        Annotated[str, StringConstraints(min_length=1, max_length=100)],
    ] = Field(
        default_factory=dict,
        description="Translated menu labels by language tag (e.g. `de`); `menu_label` is the "
        "English fallback.",
    )


def _duplicates(values: Iterable[str]) -> list[str]:
    return sorted(value for value, count in Counter(values).items() if count > 1)


class ModuleDescriptor(ContractModel):
    """Self-description of a service, served at ``/.well-known/mowdeck-module``."""

    name: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9\-]*$", max_length=64)]
    version: SemVer
    permissions: list[PermissionDef] = Field(default_factory=list)
    features: list[FeatureDef] = Field(default_factory=list)
    events: list[EventDef] = Field(default_factory=list)
    ui_areas: list[UiAreaDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_keys(self) -> Self:
        problems = {
            "permissions": _duplicates(p.key for p in self.permissions),
            "features": _duplicates(f.key for f in self.features),
            "events": _duplicates(f"{e.type}@{e.version}" for e in self.events),
            "ui_areas": _duplicates(u.route for u in self.ui_areas),
        }
        found = {field: dups for field, dups in problems.items() if dups}
        if found:
            msg = f"duplicate entries: {found}"
            raise ValueError(msg)
        return self

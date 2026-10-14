"""The collaborators every use case needs, bundled once in ``main``."""

from dataclasses import dataclass

from mowdeck_authz import Authorizer
from mowdeck_service_kit.entitlements import Entitlements

from identity.application.access import AccessChecker
from identity.application.catalogue import CatalogueService
from identity.application.context import IdentityConfig
from identity.application.keys import KeyManager
from identity.application.ports import Clock, EventPublisher, PasswordHasher, Store
from identity.application.sync import AuthzSync
from identity.application.work import UnitOfWork


@dataclass(slots=True)
class Deps:
    """Shared collaborators of the use cases."""

    store: Store
    uow: UnitOfWork
    sync: AuthzSync
    access: AccessChecker
    authorizer: Authorizer
    catalogue: CatalogueService
    keys: KeyManager
    hasher: PasswordHasher
    events: EventPublisher
    clock: Clock
    config: IdentityConfig
    entitlements: Entitlements | None = None
    """Plans (ADR 0028): the ``plan`` claim at token issue and ``max_users``; None: no gate."""

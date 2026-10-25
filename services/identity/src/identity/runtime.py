"""Wiring shared by the HTTP app (``main``) and the operator CLI (``cli``).

:class:`Runtime` builds the collaborators from the settings (or test overrides) synchronously,
so the app can hand the key manager to its token verifier before startup; :meth:`Runtime.start`
loads the keys and the catalogue and returns the wired use cases.
"""

from dataclasses import dataclass, field

import httpx
from mowdeck_authz import (
    Authorizer,
    AuthzSettings,
    FgaConnection,
    OpenFgaAuthorizer,
    OpenFgaTupleBackend,
    TupleBackend,
)
from mowdeck_contracts.v1 import utc_now
from mowdeck_service_kit.bus import EventBus
from mowdeck_service_kit.db import create_engine
from mowdeck_service_kit.entitlements import Entitlements, EntitlementsClient, client_from_settings
from prometheus_client import CollectorRegistry
from sqlalchemy.ext.asyncio import AsyncEngine

from identity.adapters.apple import AppleOAuthClient
from identity.adapters.bus.publisher import BusEventPublisher
from identity.adapters.crypto import AesGcmKeyCipher, Argon2PasswordHasher, parse_kek
from identity.adapters.db.store import SqlStore
from identity.adapters.github import GitHubOAuthClient
from identity.adapters.modules import HttpModuleSource
from identity.adapters.webauthn import PyWebAuthnCeremonies
from identity.application.access import AccessChecker
from identity.application.catalogue import CatalogueService
from identity.application.deps import Deps
from identity.application.keys import KeyManager
from identity.application.ports import Clock, ModuleSource, OAuthClient, PasswordHasher, Store
from identity.application.services import Services, build_services
from identity.application.sync import AuthzSync
from identity.application.work import UnitOfWork
from identity.module import DESCRIPTOR
from identity.settings import Settings


def key_cipher(settings: Settings) -> AesGcmKeyCipher:
    """The cipher of the signing keys (``ValueError`` without a valid key-encryption key)."""
    if settings.identity_key_encryption_key is None:
        msg = "MOWDECK_IDENTITY_KEY_ENCRYPTION_KEY (or _FILE) is required"
        raise ValueError(msg)
    return AesGcmKeyCipher(parse_kek(settings.identity_key_encryption_key.get_secret_value()))


@dataclass
class Runtime:
    """Collaborators of Identity; defaults are PostgreSQL, OpenFGA and Argon2id."""

    settings: Settings
    store: Store | None = None
    authorizer: Authorizer | None = None
    tuple_backend: TupleBackend | None = None
    module_source: ModuleSource | None = None
    hasher: PasswordHasher | None = None
    clock: Clock = utc_now
    entitlements: Entitlements | None = None
    github_transport: httpx.AsyncBaseTransport | None = None
    """Tests: a fake GitHub (``httpx.MockTransport``) behind the configured endpoints."""
    apple_transport: httpx.AsyncBaseTransport | None = None
    """Tests: a fake Apple (``httpx.MockTransport``) behind the configured endpoints."""
    engine: AsyncEngine | None = field(default=None, init=False)
    connection: FgaConnection | None = field(default=None, init=False)
    keys: KeyManager = field(init=False)
    catalogue: CatalogueService = field(init=False)
    services: Services | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        settings = self.settings
        cipher = key_cipher(settings)
        if self.store is None:
            self.engine = create_engine(settings)
            self.store = SqlStore(self.engine)
        if self.authorizer is None or self.tuple_backend is None:
            self.connection = FgaConnection(AuthzSettings.from_env())
            self.tuple_backend = self.tuple_backend or OpenFgaTupleBackend(self.connection)
        if self.hasher is None:
            self.hasher = Argon2PasswordHasher(
                settings.identity_argon2_profile, settings.identity_argon2_concurrency
            )
        if self.module_source is None and settings.module_urls:
            self.module_source = HttpModuleSource(settings.module_urls)
        self.keys = KeyManager(self.store, cipher, self.clock, settings.to_config())
        self.catalogue = CatalogueService(self.store, DESCRIPTOR, self.clock, self.module_source)

    def ensure_authorizer(self, registry: CollectorRegistry | None = None) -> Authorizer:
        """The authorizer; an ``OpenFgaAuthorizer`` on the shared connection unless given."""
        if self.authorizer is None:
            assert self.connection is not None  # noqa: S101 - set in __post_init__
            self.authorizer = OpenFgaAuthorizer(self.connection, registry=registry)
        return self.authorizer

    async def start(self, bus: EventBus | None = None) -> Services:
        """Load or create the keys, merge the catalogue, wire the use cases."""
        assert self.store is not None and self.tuple_backend is not None  # noqa: S101
        assert self.hasher is not None  # noqa: S101 - set in __post_init__
        authorizer = self.ensure_authorizer()
        caches = [authorizer] if hasattr(authorizer, "invalidate_subject") else []
        sync = AuthzSync(self.tuple_backend, self.clock, caches=caches)  # type: ignore[arg-type]
        events = BusEventPublisher(bus, producer=self.settings.service_name)
        await self.keys.ensure()
        await self.catalogue.refresh(fetch_modules=False)
        config = self.settings.to_config()
        if self.entitlements is None:
            self.entitlements = client_from_settings(self.settings)
        if isinstance(self.entitlements, EntitlementsClient):
            self.entitlements.attach(bus)
        deps = Deps(
            store=self.store,
            uow=UnitOfWork(self.store, sync, self.catalogue, events, self.clock),
            sync=sync,
            access=AccessChecker(authorizer, sync),
            authorizer=authorizer,
            catalogue=self.catalogue,
            keys=self.keys,
            hasher=self.hasher,
            events=events,
            clock=self.clock,
            config=config,
            entitlements=self.entitlements,
        )
        self.services = build_services(
            deps,
            issuer_name=self.settings.identity_issuer,
            audience=self.settings.identity_audience,
            ip_per_minute=self.settings.identity_login_ip_per_minute,
            ip_burst=self.settings.identity_login_ip_burst,
            ceremonies=PyWebAuthnCeremonies(),
            oauth=self.oauth_clients(),
        )
        return self.services

    def oauth_clients(self) -> list[tuple[OAuthClient, str]]:
        """The configured login providers with their callback URLs (ADR 0051, 0052)."""
        clients: list[tuple[OAuthClient, str]] = []
        github = self.settings.github_login()
        if github is not None:
            clients.append(
                (
                    GitHubOAuthClient(
                        client_id=github.client_id,
                        client_secret=github.client_secret,
                        authorize_url=github.authorize_url,
                        token_url=github.token_url,
                        api_url=github.api_url,
                        transport=self.github_transport,
                    ),
                    github.redirect_uri,
                )
            )
        apple = self.settings.apple_login()
        if apple is not None:
            clients.append(
                (
                    AppleOAuthClient(
                        services_id=apple.services_id,
                        team_id=apple.team_id,
                        key_id=apple.key_id,
                        private_key=apple.private_key,
                        authorize_url=apple.authorize_url,
                        token_url=apple.token_url,
                        keys_url=apple.keys_url,
                        issuer=apple.issuer,
                        pkce=apple.pkce,
                        transport=self.apple_transport,
                    ),
                    apple.redirect_uri,
                )
            )
        return clients

    async def close(self) -> None:
        """Close OpenFGA and the database."""
        if self.connection is not None:
            await self.connection.close()
        if self.engine is not None:
            await self.engine.dispose()

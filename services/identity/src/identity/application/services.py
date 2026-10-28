"""All use cases of Identity, wired once (``main``) and shared by the API and the CLI."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from mowdeck_auth.issuer import TokenIssuer

from identity.application.accounts import AccountService
from identity.application.api_tokens import ApiTokenService
from identity.application.audit import AuditService
from identity.application.authenticators import (
    ApiTokenAuthenticator,
    AppleAuthenticator,
    AuthenticatorRegistry,
    GitHubAuthenticator,
    OAuthAuthenticator,
    PasskeyAuthenticator,
    PasswordAuthenticator,
)
from identity.application.deps import Deps
from identity.application.external import ExternalIdentityService
from identity.application.onboarding import OnboardingService
from identity.application.passkeys import PasskeyService
from identity.application.platform import PlatformService
from identity.application.ports import OAuthClient, PasskeyCeremonies
from identity.application.ratelimit import TokenBucketLimiter
from identity.application.roles import RoleService
from identity.application.sessions import SessionService
from identity.application.users import UserService


@dataclass(slots=True)
class Services:
    """The use cases."""

    deps: Deps
    issuer: TokenIssuer
    authenticators: AuthenticatorRegistry
    accounts: AccountService = field(init=False)
    sessions: SessionService = field(init=False)
    users: UserService = field(init=False)
    roles: RoleService = field(init=False)
    tokens: ApiTokenService = field(init=False)
    audit: AuditService = field(init=False)
    platform: PlatformService = field(init=False)
    passkeys: PasskeyService = field(init=False)
    external: ExternalIdentityService = field(init=False)
    onboarding: OnboardingService = field(init=False)
    ip_limiter: TokenBucketLimiter | None = None
    ceremonies: PasskeyCeremonies | None = None

    def __post_init__(self) -> None:
        limiter = self.ip_limiter or TokenBucketLimiter(per_minute=10, capacity=20)
        self.accounts = AccountService(self.deps)
        self.sessions = SessionService(self.deps, self.issuer, self.authenticators, limiter)
        self.users = UserService(self.deps)
        self.roles = RoleService(self.deps)
        self.tokens = ApiTokenService(self.deps)
        self.audit = AuditService(self.deps.store)
        self.platform = PlatformService(self.deps)
        self.passkeys = PasskeyService(self.deps, self.ceremonies, limiter)
        self.external = ExternalIdentityService(self.deps, self.sessions, limiter)
        self.onboarding = OnboardingService(self.deps)


def build_services(
    deps: Deps,
    *,
    issuer_name: str,
    audience: str,
    ip_per_minute: float = 10,
    ip_burst: int = 20,
    ceremonies: PasskeyCeremonies | None = None,
    oauth: Sequence[tuple[OAuthClient, str]] = (),
) -> Services:
    """Wire the use cases: token issuer on the active key, authenticators, limiter.

    ``oauth``: the enabled login providers, each as its client and its callback URL.
    """
    issuer = TokenIssuer(
        deps.keys.active_key,
        issuer=issuer_name,
        audience=audience,
        ttl=deps.config.access_token_ttl or timedelta(minutes=10),
        clock=deps.clock,
    )
    registry = AuthenticatorRegistry()
    registry.register(PasswordAuthenticator(deps.store, deps.hasher))
    registry.register(ApiTokenAuthenticator(deps.store, deps.clock))
    rp = deps.config.passkey_rp
    if rp is not None and ceremonies is not None:
        registry.register(
            PasskeyAuthenticator(
                deps.store, deps.clock, ceremonies, rp, deps.config.webauthn_timeout
            )
        )
    for client, redirect_uri in oauth:
        kind = {"github": GitHubAuthenticator, "apple": AppleAuthenticator}.get(
            client.provider, OAuthAuthenticator
        )
        registry.register(kind(client, deps.store, deps.clock, redirect_uri=redirect_uri))
    return Services(
        deps,
        issuer,
        registry,
        ip_limiter=TokenBucketLimiter(per_minute=ip_per_minute, capacity=ip_burst),
        ceremonies=ceremonies if rp is not None else None,
    )

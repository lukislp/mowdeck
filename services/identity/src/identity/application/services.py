"""All use cases of Identity, wired once (``main``) and shared by the API and the CLI."""

from dataclasses import dataclass, field
from datetime import timedelta

from mowdeck_auth.issuer import TokenIssuer

from identity.application.accounts import AccountService
from identity.application.api_tokens import ApiTokenService
from identity.application.audit import AuditService
from identity.application.authenticators import (
    ApiTokenAuthenticator,
    AuthenticatorRegistry,
    GitHubAuthenticator,
    PasswordAuthenticator,
)
from identity.application.deps import Deps
from identity.application.platform import PlatformService
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
    ip_limiter: TokenBucketLimiter | None = None

    def __post_init__(self) -> None:
        limiter = self.ip_limiter or TokenBucketLimiter(per_minute=10, capacity=20)
        self.accounts = AccountService(self.deps)
        self.sessions = SessionService(self.deps, self.issuer, self.authenticators, limiter)
        self.users = UserService(self.deps)
        self.roles = RoleService(self.deps)
        self.tokens = ApiTokenService(self.deps)
        self.audit = AuditService(self.deps.store)
        self.platform = PlatformService(self.deps)


def build_services(
    deps: Deps,
    *,
    issuer_name: str,
    audience: str,
    ip_per_minute: float = 10,
    ip_burst: int = 20,
    github_login: bool = False,
) -> Services:
    """Wire the use cases: token issuer on the active key, authenticators, limiter."""
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
    if github_login:
        registry.register(GitHubAuthenticator())
    return Services(
        deps,
        issuer,
        registry,
        ip_limiter=TokenBucketLimiter(per_minute=ip_per_minute, capacity=ip_burst),
    )

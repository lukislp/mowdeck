"""Tenant isolation: who is calling and which account they act for.

Rule: **the account id only ever comes from the verified principal**, never from the request
body, the query string or a path parameter. Every account-scoped query filters by
:func:`current_account_id`. A resource of another account is reported as ``404 Not found`` (not
403), so its existence is not revealed.

The principal is produced by a :class:`PrincipalResolver`. The default resolver rejects every
request with 401; ``mowdeck-auth`` provides the JWT resolver in a later task.

Permission checks of :func:`require_permission` are answered by the :class:`PermissionChecker`
on ``app.state.permission_checker`` when one is installed (``mowdeck_authz.fastapi.install_authz``
installs an OpenFGA backed one); without it the token level ``Principal.permissions`` are used.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Protocol, runtime_checkable
from uuid import UUID

import structlog
from fastapi import Depends, Request
from mowdeck_contracts.v1 import Principal

from mowdeck_service_kit.errors import Forbidden, Unauthorized

_PRINCIPAL_STATE_KEY = "mowdeck_principal"


@runtime_checkable
class PrincipalResolver(Protocol):
    """Turns an incoming request into a verified :class:`Principal`.

    Implementations raise :class:`~mowdeck_service_kit.errors.Unauthorized` when the request
    carries no valid credentials.
    """

    async def resolve(self, request: Request) -> Principal:
        """Return the principal of ``request`` or raise ``Unauthorized``."""
        ...


@runtime_checkable
class PermissionChecker(Protocol):
    """Answers ``require_permission`` checks; installed on ``app.state.permission_checker``.

    The kit cannot depend on ``mowdeck-authz`` (it is the other way round), so the authorization
    library plugs in through this protocol.
    """

    async def has_permission(self, principal: Principal, permission: str) -> bool:
        """True if ``principal`` holds ``permission`` (a key such as ``mower.control``)."""
        ...


class DenyAllResolver:
    """Default resolver: no authentication is configured, so every request is unauthorized."""

    async def resolve(self, request: Request) -> Principal:
        """Always raise :class:`Unauthorized`."""
        raise Unauthorized("authentication is not configured for this service")


async def current_principal(request: Request) -> Principal:
    """FastAPI dependency returning the verified principal (401 if there is none).

    The principal is resolved once per request and cached on ``request.state``. Its subject and
    account id are bound to the log context.
    """
    cached = getattr(request.state, _PRINCIPAL_STATE_KEY, None)
    if isinstance(cached, Principal):
        return cached
    resolver: PrincipalResolver = (
        getattr(request.app.state, "principal_resolver", None) or DenyAllResolver()
    )
    principal = await resolver.resolve(request)
    setattr(request.state, _PRINCIPAL_STATE_KEY, principal)
    structlog.contextvars.bind_contextvars(
        principal=str(principal.subject),
        account_id=str(principal.account_id) if principal.account_id else None,
    )
    return principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
"""Annotated dependency: ``principal: CurrentPrincipal``."""


async def current_account_id(principal: CurrentPrincipal) -> UUID:
    """FastAPI dependency returning the principal's account id (403 if not bound to one).

    Use this, and only this, as the account filter of every account-scoped query.
    """
    if principal.account_id is None:
        raise Forbidden("this operation requires an account-scoped principal")
    return principal.account_id


CurrentAccountId = Annotated[UUID, Depends(current_account_id)]
"""Annotated dependency: ``account_id: CurrentAccountId``."""


def require_permission(permission: str) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: 403 unless the principal holds ``permission``.

    Delegates to the :class:`PermissionChecker` on ``app.state.permission_checker`` when one is
    installed (``mowdeck-authz`` checks the key on the principal's account in OpenFGA). Without
    one, the token level ``Principal.permissions`` list decides. Object-level checks use
    ``mowdeck_authz.fastapi.require`` instead.

    Usage::

        CanControl = Annotated[Principal, Depends(require_permission("mower.control"))]

        @router.post("/mowers/{mower_id}/start")
        async def start(mower_id: UUID, principal: CanControl) -> None: ...
    """

    async def dependency(request: Request, principal: CurrentPrincipal) -> Principal:
        checker: PermissionChecker | None = getattr(request.app.state, "permission_checker", None)
        if checker is not None:
            allowed = await checker.has_permission(principal, permission)
        else:
            allowed = principal.has_permission(permission)
        if not allowed:
            raise Forbidden(f"missing permission {permission}")
        return principal

    dependency.__name__ = f"require_permission_{permission.replace('.', '_')}"
    return dependency

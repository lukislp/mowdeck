"""FastAPI integration (install the ``fastapi`` extra).

::

    from mowdeck_authz import Relation
    from mowdeck_authz.fastapi import install_authz, path_object, require_or_not_found

    mower_ref = path_object("mower", "mower_id")
    CanControl = Annotated[
        Principal, Depends(require_or_not_found(Relation.CAN_CONTROL, mower_ref))
    ]

    @router.post("/mowers/{mower_id}/commands")
    async def send(mower_id: UUID, principal: CanControl, account_id: CurrentAccountId): ...

    app = create_app(settings, descriptor, routers=[router], on_shutdown=[close_authorizer])
    install_authz(app, OpenFgaAuthorizer.from_env(registry=app.state.metrics_registry))

The handler still loads the row with ``WHERE id = :id AND account_id = :account_id``: the FGA
check is the second of three isolation layers, not a replacement for the account filter.
"""

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from mowdeck_contracts.v1 import Principal, PrincipalKind
from mowdeck_service_kit.errors import Forbidden, NotFound, ServiceUnavailable
from mowdeck_service_kit.health import ReadinessCheck, add_readiness_check
from mowdeck_service_kit.tenancy import current_principal

from mowdeck_authz.authorizer import Authorizer, CheckItem, SupportsPing, readiness_check_authz
from mowdeck_authz.permissions import PERMISSION_RELATIONS, Relation
from mowdeck_authz.refs import ObjectRef, account, principal_subject

_log = logging.getLogger("mowdeck_authz.fastapi")

ObjectResolver = Callable[[Request], Awaitable[ObjectRef] | ObjectRef]
"""Finds the object a request is about, e.g. from a path parameter."""


async def get_authorizer(request: Request) -> Authorizer:
    """Dependency: the authorizer on ``app.state.authorizer`` (503 if none is installed)."""
    authorizer: Authorizer | None = getattr(request.app.state, "authorizer", None)
    if authorizer is None:
        raise ServiceUnavailable("authorization is not configured for this service")
    return authorizer


AuthzDependency = Annotated[Authorizer, Depends(get_authorizer)]
"""Annotated dependency: ``authz: AuthzDependency``."""


PREFILTER_KEYS: dict[str, str] = {
    **{entry.computed.value: key for key, entry in PERMISSION_RELATIONS.items()},
    Relation.CAN_EMERGENCY_STOP.value: "mower.view",
}
"""Computed relation to the permission key a token must list for the pre-filter to pass."""


def prefilter_denies(principal: Principal, relation: str) -> bool:
    """True if the token's ``perms`` (an upper bound) rule out ``relation`` without asking.

    The pre-filter only ever denies: a key in ``perms`` still needs the OpenFGA answer.
    Relations without a permission key (``member``, ``can_manage_account``) are never filtered.
    """
    key = PREFILTER_KEYS.get(str(relation))
    return key is not None and key not in principal.permissions


class AuthzPermissionChecker:
    """Kit ``PermissionChecker``: a permission key is checked on the principal's account.

    ``require_permission("mower.control")`` becomes ``can_control`` on ``account:<acc>``. Unknown
    keys, principals without an account and service principals are denied. With
    ``token_prefilter`` a key missing from the token's ``perms`` is denied without a call.
    """

    def __init__(self, authorizer: Authorizer, *, token_prefilter: bool = False) -> None:
        self.authorizer = authorizer
        self.token_prefilter = token_prefilter

    async def has_permission(self, principal: Principal, permission: str) -> bool:
        """Check the computed relation of ``permission`` on the principal's account."""
        entry = PERMISSION_RELATIONS.get(permission)
        if entry is None:
            _log.warning("unknown permission key; denying", extra={"permission": permission})
            return False
        if principal.account_id is None or principal.kind == PrincipalKind.SERVICE:
            return False
        if self.token_prefilter and permission not in principal.permissions:
            return False
        return await self.authorizer.check(
            principal_subject(principal), entry.computed, account(principal.account_id)
        )


def install_authz(
    app: FastAPI,
    authorizer: Authorizer,
    *,
    readiness: bool = True,
    token_prefilter: bool = False,
) -> None:
    """Put ``authorizer`` on ``app.state.authorizer`` and make ``require_permission`` use it.

    Also registers the ``authz`` readiness check when the authorizer supports ``ping()``.
    ``OpenFgaAuthorizer`` connects lazily, so no startup hook is needed; add
    :func:`close_authorizer` to ``on_shutdown``.

    ``token_prefilter=True`` enables the token pre-filter: a relation whose permission key is
    not in the principal's ``perms`` claim is denied without asking OpenFGA. Enable it only when
    principals come from Identity's tokens (``mowdeck-auth``), whose ``perms`` list every key
    held anywhere in the account; a newly granted key then takes effect with the next token.
    """
    app.state.authorizer = authorizer
    app.state.authz_token_prefilter = token_prefilter
    app.state.permission_checker = AuthzPermissionChecker(
        authorizer, token_prefilter=token_prefilter
    )
    ping = getattr(authorizer, "ping", None)
    if readiness and ping is not None and inspect.iscoroutinefunction(ping):
        supports_ping: SupportsPing = authorizer  # type: ignore[assignment]
        add_readiness_check(app, ReadinessCheck("authz", readiness_check_authz(supports_ping)))


async def close_authorizer(app: FastAPI) -> None:
    """Shutdown hook: close the installed authorizer if it has ``close()``."""
    close = getattr(getattr(app.state, "authorizer", None), "close", None)
    if close is not None:
        await close()


def path_object(object_type: str, param: str) -> ObjectResolver:
    """Resolver for ``/.../{param}``: the path parameter must be a UUID (else 404)."""

    def resolve(request: Request) -> ObjectRef:
        raw = request.path_params.get(param)
        try:
            object_id = UUID(str(raw))
        except ValueError:
            raise NotFound(f"{object_type} not found") from None
        return ObjectRef(object_type, str(object_id))

    return resolve


async def principal_account(request: Request) -> ObjectRef:
    """Resolver for account-wide endpoints: ``account:<principal.account_id>`` (403 if none)."""
    principal = await current_principal(request)
    if principal.account_id is None:
        raise Forbidden("this operation requires an account-scoped principal")
    return account(principal.account_id)


async def _resolve(resolver: ObjectResolver, request: Request) -> ObjectRef:
    result = resolver(request)
    if inspect.isawaitable(result):
        return await result
    return result


async def authorize(
    request: Request,
    relation: str,
    obj: ObjectRef,
    *,
    hide: bool = True,
    visible_relation: str = Relation.CAN_VIEW,
) -> Principal:
    """Check ``relation`` on ``obj`` for the request's principal; the relation may be chosen at
    runtime (for example from the kind of a command in the request body).

    ``hide=True`` behaves like :func:`require_or_not_found`: ``relation`` and
    ``visible_relation`` are asked in one batch; allowed gives the principal, visible but not
    allowed gives **403**, neither gives **404**. ``hide=False`` behaves like :func:`require`
    (**403** when not allowed). 401 without a principal.
    """
    principal = await current_principal(request)
    if not hide:
        (allowed,) = await _allowed(request, principal, [relation], obj)
        if not allowed:
            raise Forbidden(f"missing {relation} on {obj.type}")
        return principal
    relations = [relation] if relation == visible_relation else [relation, visible_relation]
    answers = await _allowed(request, principal, relations, obj)
    if answers[0]:
        return principal
    if len(answers) > 1 and answers[1]:
        raise Forbidden(f"missing {relation} on {obj.type}")
    raise NotFound(f"{obj.type} not found")


def require(relation: str, object_resolver: ObjectResolver) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: the principal, or **403** unless it has ``relation`` on the object.

    401 without a principal (from the kit's resolver). Service principals get 403. Use this when
    revealing that the object exists is fine (account-wide resources, own objects).
    """

    async def dependency(request: Request) -> Principal:
        await current_principal(request)
        obj = await _resolve(object_resolver, request)
        return await authorize(request, relation, obj, hide=False)

    dependency.__name__ = f"require_{relation}"
    return dependency


def require_or_not_found(
    relation: str,
    object_resolver: ObjectResolver,
    *,
    visible_relation: str = Relation.CAN_VIEW,
) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory that hides objects the principal cannot see.

    One batch check asks ``relation`` and ``visible_relation`` (default ``can_view``):
    allowed gives the principal, visible but not allowed gives **403**, neither gives **404**, so
    a foreign object never reveals its existence (the kit's tenancy rule).
    """

    async def dependency(request: Request) -> Principal:
        await current_principal(request)
        obj = await _resolve(object_resolver, request)
        return await authorize(request, relation, obj, visible_relation=visible_relation)

    dependency.__name__ = f"require_{relation}_or_not_found"
    return dependency


async def _allowed(
    request: Request, principal: Principal, relations: list[str], obj: ObjectRef
) -> list[bool]:
    """Answers for ``relations`` on ``obj``; all False for principals without object rights."""
    authorizer = await get_authorizer(request)
    try:
        subject = principal_subject(principal)
    except ValueError:
        return [False] * len(relations)
    answers = [False] * len(relations)
    prefilter = getattr(request.app.state, "authz_token_prefilter", False) is True
    asked = [
        i for i, rel in enumerate(relations) if not (prefilter and prefilter_denies(principal, rel))
    ]
    if not asked:
        return answers
    if len(asked) == 1:
        answers[asked[0]] = await authorizer.check(subject, relations[asked[0]], obj)
        return answers
    items: list[Any] = [CheckItem(subject, relations[i], obj) for i in asked]
    for index, allowed in zip(asked, await authorizer.check_many(items), strict=True):
        answers[index] = allowed
    return answers

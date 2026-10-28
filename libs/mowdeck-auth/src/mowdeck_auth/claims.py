"""Access token claims and their mapping to :class:`~mowdeck_contracts.v1.Principal`.

| Claim | Principal field | Content |
| --- | --- | --- |
| ``sub`` | ``subject`` | user id, or API token id |
| ``acc`` | ``account_id`` | account the token is scoped to (absent for operators and services) |
| ``kind`` | ``kind`` | ``user``, ``subuser``, ``api_token``, ``service`` |
| ``name`` | ``display_name`` | display name, at most 64 characters |
| ``roles`` | ``roles`` | role keys in the account (informational) |
| ``perms`` | ``permissions`` | keys held anywhere in the account (an upper bound) |
| ``plan`` | ``plan`` | plan key of the account |
| ``sid`` | ``session_id`` | login session (absent for API tokens) |
| ``jti`` | ``token_id`` | unique token id |
| ``auth_time`` | ``auth_time`` | epoch seconds of the last login or step-up (not for API tokens) |
| ``amr`` | ``auth_methods`` | how: ``pwd``, ``passkey``, ``github``, ``apple`` |

Plus the registered claims ``iss``, ``aud``, ``iat``, ``nbf``, ``exp``.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import Principal

REQUIRED_CLAIMS: tuple[str, ...] = ("sub", "kind", "iss", "aud", "iat", "exp", "jti")
"""Claims every access token must carry."""

MAX_NAME_LENGTH = 64
"""Display names are truncated to keep tokens small."""


def principal_claims(principal: Principal) -> dict[str, Any]:
    """The MowDeck specific claims of ``principal`` (no registered time claims)."""
    claims: dict[str, Any] = {
        "sub": str(principal.subject),
        "kind": principal.kind.value,
        "roles": list(principal.roles),
        "perms": list(principal.permissions),
    }
    if principal.account_id is not None:
        claims["acc"] = str(principal.account_id)
    if principal.display_name:
        claims["name"] = principal.display_name[:MAX_NAME_LENGTH]
    if principal.plan:
        claims["plan"] = principal.plan
    if principal.session_id is not None:
        claims["sid"] = str(principal.session_id)
    if principal.token_id is not None:
        claims["jti"] = str(principal.token_id)
    if principal.auth_time is not None:
        claims["auth_time"] = int(principal.auth_time.timestamp())
        claims["amr"] = list(principal.auth_methods)
    return claims


def _timestamp(value: Any, claim: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"claim {claim} must be a number"
        raise ValueError(msg)
    return datetime.fromtimestamp(value, UTC)


def _uuid(value: Any, claim: str) -> UUID | None:
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"claim {claim} must be a string"
        raise ValueError(msg)
    return UUID(value)


def _strings(value: Any, claim: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = f"claim {claim} must be a list of strings"
        raise ValueError(msg)
    return list(value)


def principal_from_claims(claims: Mapping[str, Any]) -> Principal:
    """Build the principal from verified claims (``ValueError`` if they are malformed)."""
    return Principal.model_validate(
        {
            "subject": _uuid(claims.get("sub"), "sub"),
            "account_id": _uuid(claims.get("acc"), "acc"),
            "kind": claims.get("kind"),
            "display_name": claims.get("name"),
            "roles": _strings(claims.get("roles"), "roles"),
            "permissions": _strings(claims.get("perms"), "perms"),
            "plan": claims.get("plan"),
            "token_id": _uuid(claims.get("jti"), "jti"),
            "session_id": _uuid(claims.get("sid"), "sid"),
            "auth_time": _timestamp(claims.get("auth_time"), "auth_time"),
            "auth_methods": _strings(claims.get("amr"), "amr"),
        }
    )

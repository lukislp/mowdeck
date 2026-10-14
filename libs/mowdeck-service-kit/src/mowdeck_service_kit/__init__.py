"""MowDeck service kit: the skeleton every MowDeck service is built on.

Start with :func:`create_app`; see the package README for a complete example.
"""

from mowdeck_service_kit.app import API_PREFIX, Bus, LifecycleHook, create_app, get_bus, run
from mowdeck_service_kit.errors import (
    BadRequest,
    Conflict,
    Forbidden,
    NotFound,
    ServiceError,
    ServiceUnavailable,
    Unauthorized,
    ValidationFailed,
)
from mowdeck_service_kit.health import ReadinessCheck, add_readiness_check
from mowdeck_service_kit.logging import configure_logging, get_logger
from mowdeck_service_kit.settings import ServiceSettings
from mowdeck_service_kit.tenancy import (
    CurrentAccountId,
    CurrentPrincipal,
    PermissionChecker,
    PrincipalResolver,
    current_account_id,
    current_principal,
    require_permission,
)

__version__ = "0.1.0"

__all__ = [
    "API_PREFIX",
    "BadRequest",
    "Bus",
    "Conflict",
    "CurrentAccountId",
    "CurrentPrincipal",
    "Forbidden",
    "LifecycleHook",
    "NotFound",
    "PermissionChecker",
    "PrincipalResolver",
    "ReadinessCheck",
    "ServiceError",
    "ServiceSettings",
    "ServiceUnavailable",
    "Unauthorized",
    "ValidationFailed",
    "add_readiness_check",
    "configure_logging",
    "create_app",
    "current_account_id",
    "current_principal",
    "get_bus",
    "get_logger",
    "require_permission",
    "run",
]

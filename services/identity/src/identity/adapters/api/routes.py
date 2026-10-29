"""Routers of identity under ``/api/v1/identity``."""

from identity.adapters.api.account_routes import router as account_router
from identity.adapters.api.auth_routes import jwks_document
from identity.adapters.api.auth_routes import router as auth_router
from identity.adapters.api.demo_routes import DEMO_POLICY
from identity.adapters.api.demo_routes import router as demo_router
from identity.adapters.api.oauth_routes import router as oauth_router
from identity.adapters.api.passkey_routes import router as passkey_router
from identity.adapters.api.platform_routes import router as platform_router
from identity.adapters.api.security_routes import router as security_router

ROUTERS = [
    auth_router,
    passkey_router,
    oauth_router,
    security_router,
    account_router,
    platform_router,
    demo_router,
]
"""Mounted under ``/api/v1`` by the kit."""

__all__ = ["DEMO_POLICY", "ROUTERS", "jwks_document"]

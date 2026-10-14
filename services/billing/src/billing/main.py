"""Entry point of billing: wires the layers into one FastAPI app.

``python -m billing.main`` (the container command) loads the settings from the environment,
builds the app and serves it. Tests call :func:`build_app` with their own settings, store,
provider, authorizer, bus, plan catalogue, clock and principal resolver.

Billing runs only where billing is enabled (Compose profile ``billing``); there is no switch
inside. Startup: read the price file, subscribe ``identity.account.created``. Readiness: the bus
(kit), the database and OpenFGA.
"""

from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI
from mowdeck_auth import principal_resolver_from_env
from mowdeck_authz import Authorizer, AuthzSettings, FgaConnection, OpenFgaAuthorizer
from mowdeck_authz.events import subscribe_cache_invalidation
from mowdeck_authz.fastapi import install_authz
from mowdeck_contracts.v1 import utc_now
from mowdeck_service_kit import PrincipalResolver, create_app, get_logger, run
from mowdeck_service_kit.bus import EventBus, Subscription
from mowdeck_service_kit.db import create_engine, readiness_check_db

from billing.adapters.api.deps import install_error_mapping
from billing.adapters.api.routes import router
from billing.adapters.bus.entitlements import BusEntitlementsGateway
from billing.adapters.bus.publisher import BusEventPublisher
from billing.adapters.bus.subscriptions import subscribe_all
from billing.adapters.db.store import SqlStore
from billing.adapters.plans import HttpPlanCatalog, StaticPlanCatalog
from billing.adapters.providers.fake import FakeProvider
from billing.adapters.providers.stripe import StripeProvider
from billing.application.billing import BillingConfig, BillingService
from billing.application.ports import (
    Clock,
    EntitlementsGateway,
    PaymentProvider,
    PlanCatalog,
    Store,
)
from billing.domain.prices import PriceTable, parse_price_file
from billing.module import DESCRIPTOR
from billing.settings import Settings

# Named explicitly: under "python -m" the module is called __main__.
_log = get_logger("billing.main")


def load_prices(settings: Settings) -> PriceTable:
    """The price file plus ``MOWDECK_BILLING_PRICE_IDS``; a missing file sells nothing.

    An invalid file raises ``InvalidInput``: better no start than wrong prices.
    """
    path = Path(settings.billing_prices_file)
    table = PriceTable()
    if settings.billing_prices_file and path.is_file():
        table = parse_price_file(path.read_text(encoding="utf-8"))
    else:
        _log.warning("price file not found", path=settings.billing_prices_file)
    table = table.with_overrides(settings.billing_price_ids)
    _log.info("prices loaded", currency=table.currency, plans=sorted(table.sold))
    return table


def build_provider(settings: Settings) -> PaymentProvider:
    """The configured payment provider."""
    secret = settings.billing_stripe_webhook_secret
    webhook_secret = secret.get_secret_value() if secret else ""
    if not webhook_secret:
        _log.warning("MOWDECK_BILLING_STRIPE_WEBHOOK_SECRET is not set: webhooks are refused")
    if settings.billing_provider == "fake":
        _log.warning("billing runs with the fake provider: nothing is charged")
        return FakeProvider(webhook_secret=webhook_secret)
    key = settings.billing_stripe_secret_key
    if key is None or not key.get_secret_value():
        msg = "MOWDECK_BILLING_STRIPE_SECRET_KEY (or _FILE) is required for the stripe provider"
        raise ValueError(msg)
    return StripeProvider(
        key.get_secret_value(),
        webhook_secret,
        api_base=settings.billing_stripe_api_base,
        automatic_tax=settings.billing_automatic_tax,
    )


def build_app(
    settings: Settings,
    *,
    bus: EventBus | None = None,
    store: Store | None = None,
    provider: PaymentProvider | None = None,
    authorizer: Authorizer | None = None,
    principal_resolver: PrincipalResolver | None = None,
    entitlements: EntitlementsGateway | None = None,
    catalog: PlanCatalog | None = None,
    prices: PriceTable | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Build the app. Without overrides it uses PostgreSQL, OpenFGA, NATS and the provider."""
    engine = None
    if store is None:
        if settings.database_url is None:
            msg = "MOWDECK_DATABASE_URL (or _FILE) is required"
            raise ValueError(msg)
        engine = create_engine(settings)
        store = SqlStore(engine)
    active_provider = provider or build_provider(settings)
    table = prices if prices is not None else load_prices(settings)
    plan_catalog = catalog
    if plan_catalog is None:
        plan_catalog = (
            HttpPlanCatalog(settings.billing_entitlements_url)
            if settings.billing_entitlements_url
            else StaticPlanCatalog()
        )
    connection: FgaConnection | None = None
    if authorizer is None:
        connection = FgaConnection(AuthzSettings.from_env())
    store_ref: Store = store
    catalog_ref: PlanCatalog = plan_catalog

    async def startup(app: FastAPI) -> None:
        active_bus: EventBus | None = app.state.bus
        service = BillingService(
            store=store_ref,
            provider=active_provider,
            entitlements=entitlements
            or BusEntitlementsGateway(
                active_bus, timeout=settings.billing_entitlements_timeout_seconds
            ),
            catalog=catalog_ref,
            events=BusEventPublisher(active_bus, producer=settings.service_name),
            config=BillingConfig(
                prices=table,
                grace_days=settings.billing_grace_days,
                automatic_tax=settings.billing_automatic_tax,
                app_url=settings.billing_app_url,
                price_cache_seconds=settings.billing_price_cache_seconds,
            ),
            clock=clock or utc_now,
        )
        app.state.billing_service = service
        subscriptions: list[Subscription] = []
        if active_bus is not None:
            subscriptions = await subscribe_all(active_bus, service)
            cache = app.state.authorizer
            if hasattr(cache, "invalidate_subject"):
                subscriptions.append(await subscribe_cache_invalidation(active_bus, cache))
        app.state.subscriptions = subscriptions
        _log.info("billing started", provider=active_provider.name, plans=sorted(table.sold))

    async def shutdown(app: FastAPI) -> None:
        for subscription in getattr(app.state, "subscriptions", []):
            await subscription.unsubscribe()
        if connection is not None:
            await connection.close()
        if engine is not None:
            await engine.dispose()

    app = create_app(
        settings,
        DESCRIPTOR,
        routers=[router],
        readiness_checks=[readiness_check_db(engine)] if engine is not None else [],
        on_startup=[startup],
        on_shutdown=[shutdown],
        bus=bus,
        principal_resolver=principal_resolver,
    )
    if authorizer is None:
        assert connection is not None  # noqa: S101 - set above
        authorizer = OpenFgaAuthorizer(connection, registry=app.state.metrics_registry)
    install_authz(app, authorizer)
    install_error_mapping(app)
    return app


def default_principal_resolver(
    environ: Mapping[str, str] | None = None,
) -> PrincipalResolver | None:
    """The JWT resolver when ``MOWDECK_AUTH_JWKS_URL`` is set, else None (the kit denies all)."""
    resolver = principal_resolver_from_env(environ)
    if resolver is None:
        _log.warning("MOWDECK_AUTH_JWKS_URL is not set: every account request gets 401")
    return resolver


def main() -> None:
    """Load the settings from the environment and serve the app."""
    settings = Settings.from_env()
    run(build_app(settings, principal_resolver=default_principal_resolver()), settings)


if __name__ == "__main__":
    main()

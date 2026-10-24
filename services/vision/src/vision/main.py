"""Entry point of vision: wires the layers into one FastAPI app.

``python -m vision.main`` (the container command) loads the settings from the environment,
builds the app and serves it. Tests call :func:`build_app` with their own settings, store,
provider factory, authorizer, bus, entitlements, clock and principal resolver.

Startup: subscribe the mower view (telemetry, maps, commands, results), the automatic analysis
(durable `vision-auto`, unless switched off) and Identity's user
events, start the sweep, warm LiteLLM up in a thread (its import is slow). Readiness: the bus
(kit), the database and OpenFGA.
"""

import asyncio
import contextlib
from collections.abc import Mapping

from fastapi import FastAPI
from mowdeck_auth import principal_resolver_from_env
from mowdeck_authz import Authorizer, AuthzSettings, FgaConnection, OpenFgaAuthorizer
from mowdeck_authz.events import subscribe_cache_invalidation
from mowdeck_authz.fastapi import install_authz
from mowdeck_contracts.v1 import utc_now
from mowdeck_service_kit import PrincipalResolver, create_app, get_logger, run
from mowdeck_service_kit.bus import EventBus, Subscription
from mowdeck_service_kit.db import create_engine, readiness_check_db
from mowdeck_service_kit.entitlements import (
    Entitlements,
    EntitlementsClient,
    client_from_settings,
)

from vision.adapters.api.deps import install_error_mapping
from vision.adapters.api.routes import router
from vision.adapters.authz import AuthzDriveChecker
from vision.adapters.bus.gateways import BusFleetGateway, BusPhotoSource, BusPublisher
from vision.adapters.bus.subscriptions import subscribe_all
from vision.adapters.bus.view import BusMowerView
from vision.adapters.crypto import AesGcmSecretBox, parse_key
from vision.adapters.db.store import SqlStore
from vision.adapters.providers.factory import DefaultProviderFactory
from vision.adapters.providers.llm import load_litellm
from vision.application.assist import AssistConfig, VisionService
from vision.application.auto import AutoAnalysis, AutoSettings
from vision.application.executor import DriveConfig, Executor
from vision.application.ports import Clock, ProviderFactory, SecretBox, Store
from vision.application.providers import HostedDefault, ProviderPolicy, ProviderService
from vision.domain.auto import AutoConfig, parse_codes
from vision.domain.context import Motion
from vision.domain.families import check_model
from vision.module import DESCRIPTOR
from vision.settings import Settings

# Named explicitly: under "python -m" the module is called __main__.
_log = get_logger("vision.main")


def secret_box(settings: Settings) -> SecretBox | None:
    """The AES-GCM box for user keys, None without ``MOWDECK_VISION_SECRETS_KEY``."""
    key = settings.vision_secrets_key
    if key is None or not key.get_secret_value().strip():
        _log.warning("MOWDECK_VISION_SECRETS_KEY is not set: users cannot store API keys")
        return None
    return AesGcmSecretBox(parse_key(key.get_secret_value()))


def hosted_default(settings: Settings) -> HostedDefault:
    """The installation default from the settings (validated when enabled)."""
    model = settings.vision_default_model.strip()
    if settings.vision_default_enabled:
        check_model(model, allow_fake=True)
    key = settings.vision_default_api_key
    return HostedDefault(
        enabled=settings.vision_default_enabled,
        model=model,
        api_base=settings.vision_default_api_base or None,
        api_key=key.get_secret_value() if key and key.get_secret_value() else None,
    )


def auto_config(settings: Settings) -> AutoConfig:
    """The limits of automatic analyses from the settings."""
    return AutoConfig(
        enabled=settings.vision_auto_enabled,
        cooldown_minutes=settings.vision_auto_cooldown_minutes,
        daily_per_mower=settings.vision_auto_daily_per_mower,
        codes=parse_codes(settings.vision_auto_error_codes),
        max_state_age_seconds=settings.vision_auto_max_state_age_seconds,
    )


def build_app(
    settings: Settings,
    *,
    bus: EventBus | None = None,
    store: Store | None = None,
    factory: ProviderFactory | None = None,
    authorizer: Authorizer | None = None,
    principal_resolver: PrincipalResolver | None = None,
    entitlements: Entitlements | None = None,
    view: BusMowerView | None = None,
    clock: Clock | None = None,
    sweep: bool = True,
    warm_litellm: bool = True,
) -> FastAPI:
    """Build the app. Without overrides it uses PostgreSQL, OpenFGA, NATS and LiteLLM."""
    engine = None
    if store is None:
        if settings.database_url is None:
            msg = "MOWDECK_DATABASE_URL (or _FILE) is required"
            raise ValueError(msg)
        engine = create_engine(settings)
        store = SqlStore(engine)
    store_ref: Store = store
    box = secret_box(settings)
    hosted = hosted_default(settings)
    plans: Entitlements = (
        entitlements if entitlements is not None else client_from_settings(settings)
    )
    provider_factory: ProviderFactory = factory or DefaultProviderFactory(
        timeout_seconds=settings.vision_provider_timeout_seconds,
        retries=settings.vision_provider_retries,
    )
    mower_view = view or BusMowerView()
    now = clock or utc_now
    connection: FgaConnection | None = None
    if authorizer is None:
        connection = FgaConnection(AuthzSettings.from_env())
    background: list[asyncio.Task[None]] = []

    async def startup(app: FastAPI) -> None:
        active_bus: EventBus | None = app.state.bus
        providers = ProviderService(
            store=store_ref,
            box=box,
            entitlements=plans,
            factory=provider_factory,
            hosted=hosted,
            policy=ProviderPolicy(
                allow_fake=settings.vision_allow_fake_provider,
                api_base_policy=settings.vision_user_api_base_policy,
                tests_per_hour=settings.vision_provider_tests_per_hour,
            ),
            clock=now,
        )
        publisher = BusPublisher(active_bus, producer=settings.service_name)
        fleet = BusFleetGateway(active_bus, timeout_seconds=settings.vision_fleet_timeout_seconds)
        executor = Executor(
            store=store_ref,
            fleet=fleet,
            frames=publisher,
            view=mower_view,
            clock=now,
            config=DriveConfig(
                rate_hz=settings.vision_frame_rate_hz,
                state_timeout_seconds=settings.vision_state_timeout_seconds,
                start_state_max_age_seconds=settings.vision_start_state_max_age_seconds,
                geofence_margin_m=settings.vision_geofence_margin_m,
                settle_seconds=settings.vision_settle_seconds,
                min_move_m=settings.vision_min_move_m,
                resume_confirm_seconds=settings.vision_resume_confirm_seconds,
                resume_confirm_move_m=settings.vision_resume_confirm_move_m,
            ),
        )
        service = VisionService(
            store=store_ref,
            providers=providers,
            factory=provider_factory,
            view=mower_view,
            photos=BusPhotoSource(
                active_bus, timeout_seconds=settings.vision_photo_timeout_seconds
            ),
            executor=executor,
            events=publisher,
            entitlements=plans,
            clock=now,
            config=AssistConfig(
                motion=Motion(
                    linear_mps_at_1=settings.vision_linear_mps_at_1,
                    turn_deg_per_s_at_1=settings.vision_turn_deg_per_s_at_1,
                ),
                proposal_ttl_seconds=settings.vision_proposal_ttl_seconds,
                max_rounds=settings.vision_max_rounds,
                min_confidence=settings.vision_min_confidence,
                user_daily_requests=settings.vision_user_daily_requests,
                hosted_account_daily_requests=settings.vision_hosted_account_daily_requests,
                keep_images_days=settings.vision_keep_images_days,
                max_images=settings.vision_max_images,
                max_image_bytes=settings.vision_max_image_bytes,
                context_radius_m=settings.vision_context_radius_m,
                dark_frame_threshold=settings.vision_dark_frame_threshold,
                light_wait_seconds=settings.vision_light_wait_seconds,
                stereo_max_skew_ms=settings.vision_stereo_max_skew_ms,
                map_image=settings.vision_map_image,
                geofence_margin_m=settings.vision_geofence_margin_m,
            ),
            fleet=fleet,
        )
        auto_limits = auto_config(settings)
        auto = AutoAnalysis(
            service=service,
            store=store_ref,
            drivers=AuthzDriveChecker(app.state.authorizer),
            entitlements=plans,
            clock=now,
            config=auto_limits,
        )
        app.state.vision_service = service
        app.state.provider_service = providers
        app.state.auto_analysis = auto
        app.state.auto_settings = AutoSettings(
            store=store_ref, entitlements=plans, clock=now, config=auto_limits
        )
        app.state.vision_status = {
            "hosted_default_enabled": hosted.enabled,
            "hosted_default_model": hosted.model if hosted.enabled else None,
            "secrets_available": box is not None,
            "allow_fake_provider": settings.vision_allow_fake_provider,
            "user_api_base_policy": settings.vision_user_api_base_policy,
        }
        subscriptions: list[Subscription] = []
        if isinstance(plans, EntitlementsClient):
            plans.attach(active_bus)
            plan_subscription = await plans.subscribe()
            if plan_subscription is not None:
                subscriptions.append(plan_subscription)
        if active_bus is not None:
            subscriptions.extend(
                await subscribe_all(
                    active_bus, mower_view, store_ref, auto if auto_limits.enabled else None
                )
            )
            cache = app.state.authorizer
            if hasattr(cache, "invalidate_subject"):
                subscriptions.append(await subscribe_cache_invalidation(active_bus, cache))
        app.state.subscriptions = subscriptions
        if sweep:
            background.append(asyncio.create_task(_sweeper(service, settings.vision_sweep_seconds)))
        if warm_litellm:  # users may configure a LiteLLM provider at any time
            background.append(asyncio.create_task(_warm_litellm()))
        _log.info(
            "vision started",
            hosted_default=hosted.model if hosted.enabled else None,
            secrets=box is not None,
            allow_fake=settings.vision_allow_fake_provider,
        )

    async def shutdown(app: FastAPI) -> None:
        for task in background:
            task.cancel()
        for task in background:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        service: VisionService | None = getattr(app.state, "vision_service", None)
        if service is not None:
            await service.close()
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


async def _sweeper(service: VisionService, interval: float) -> None:
    """Expire waiting proposals, give up interrupted work, purge images."""
    while True:
        try:
            await service.sweep()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("vision sweep failed")
        await asyncio.sleep(interval)


async def _warm_litellm() -> None:
    """Import LiteLLM in a thread so the first analysis does not wait for it."""
    try:
        await asyncio.to_thread(load_litellm)
        _log.info("litellm loaded")
    except Exception:
        _log.exception("loading litellm failed")


def default_principal_resolver(
    environ: Mapping[str, str] | None = None,
) -> PrincipalResolver | None:
    """The JWT resolver when ``MOWDECK_AUTH_JWKS_URL`` is set, else None (the kit denies all)."""
    resolver = principal_resolver_from_env(environ)
    if resolver is None:
        _log.warning("MOWDECK_AUTH_JWKS_URL is not set: every request gets 401")
    return resolver


def main() -> None:
    """Load the settings from the environment and serve the app."""
    settings = Settings.from_env()
    run(build_app(settings, principal_resolver=default_principal_resolver()), settings)


if __name__ == "__main__":
    main()

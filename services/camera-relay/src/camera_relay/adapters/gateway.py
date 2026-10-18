"""The ``VendorSessions`` port: the Device-Gateway's camera session and release, and the source
drivers.

``offer`` asks the gateway for a camera session (``gateway.mower.<id>.camera.session``, core
NATS) with the relay's service principal and the lease's account before the first lease of a
key; ``start`` hands that session (or, when the offer is older than
:data:`OFFER_REUSE_SECONDS`, a fresh one) to the source driver of its ``CameraProvider``. The
gateway checks the account like for commands, so a session of account A's key is only ever
started with account A. ``stop`` stops the driver and, with ``release``, sends
``gateway.mower.<id>.camera.release``: the mower does not stop publishing by itself.

A provider without a driver, or whose driver is not available (``agora`` without a configured or
reachable agora-bridge, step 7 of ADR 0045), is answered with ``unsupported_provider`` and the
vendor session is **not** released: a client in ``auto`` falls back to the direct path
(ADR 0032), and its viewer needs the mower's stream.

Media arrives later: the reconciler reports ``source_started`` once a camera's path is ready;
a driver whose processes fail for good reports ``source_failed``. Both reports run as tasks,
never inside ``start`` (the use case holds the session's lock while it calls the port).
"""

import asyncio
import time
from collections.abc import Callable, Sequence
from typing import Protocol
from uuid import UUID

from mowdeck_contracts.v1 import (
    CameraReleaseReply,
    CameraReleaseRequest,
    CameraSession,
    CameraSessionReply,
    CameraSessionRequest,
)
from mowdeck_contracts.v1.camera import CameraProvider, CameraSessionErrorCode
from mowdeck_contracts.v1.principal import PrincipalKind, PrincipalRef
from mowdeck_contracts.v1.subjects import camera_release_subject, camera_session_subject
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusError, EventBus

from camera_relay.adapters.media.pipeline import RunningSource
from camera_relay.application.ports import (
    ForbiddenSourceError,
    SourceError,
    SourceOffer,
    SourceUnavailableError,
    UnsupportedProviderError,
)
from camera_relay.domain.effects import StartVendorSession, StopVendorSession, UpdateCameras
from camera_relay.domain.models import RelayKey

_log = get_logger("camera_relay.gateway")

OFFER_REUSE_SECONDS = 30.0
"""How long ``start`` reuses the vendor session fetched by ``offer`` (the same attach uses it
at once; an older one is fetched again)."""


class SourceDriver(Protocol):
    """Runs the media of one provider for a started vendor session."""

    provider: CameraProvider

    async def available(self) -> bool:
        """Whether the driver can serve its provider now (a bridge may be missing)."""
        ...

    async def start(self, source: RunningSource) -> None:
        """Start publishing the source's cameras."""
        ...

    async def update(self, source: RunningSource, cameras: Sequence[str]) -> None:
        """Publish exactly ``cameras``."""
        ...

    async def stop(self, source: RunningSource) -> None:
        """Stop publishing."""
        ...


class SourceReports(Protocol):
    """Where the sources report (``RelayService``)."""

    async def source_started(
        self, key: RelayKey, session_id: UUID, source: CameraProvider, publishing: list[str]
    ) -> None:
        """The source has media."""
        ...

    async def cameras_published(self, key: RelayKey, session_id: UUID, cameras: list[str]) -> None:
        """More cameras of the running source deliver."""
        ...

    async def source_failed(self, key: RelayKey, session_id: UUID) -> None:
        """The source failed."""
        ...


def service_principal(subject: UUID) -> PrincipalRef:
    """The relay acting on its own behalf."""
    return PrincipalRef(subject=subject, kind=PrincipalKind.SERVICE, display_name="camera-relay")


class GatewayVendorSessions:
    """Vendor sessions through the Device-Gateway, media through the source drivers."""

    def __init__(
        self,
        bus: Callable[[], EventBus | None],
        drivers: Sequence[SourceDriver],
        *,
        reports: Callable[[], SourceReports],
        principal: PrincipalRef,
        timeout_seconds: float = 10.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self.drivers = {driver.provider: driver for driver in drivers}
        self._reports = reports
        self.principal = principal
        self.timeout_seconds = timeout_seconds
        self._monotonic = monotonic
        self.sources: dict[str, RunningSource] = {}
        self._offers: dict[RelayKey, tuple[float, CameraSession]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # --- VendorSessions -----------------------------------------------------------------------

    async def offer(self, key: RelayKey) -> SourceOffer:
        """Request the vendor session for the next start of ``key``; raises ``SourceError``
        (``UnsupportedProviderError`` without releasing the session)."""
        self._offers.pop(key, None)
        session = await self._camera_session(key)
        driver = self.drivers.get(session.provider)
        if driver is None or not await driver.available():
            raise _unsupported(session.provider)
        self._offers[key] = (self._monotonic(), session)
        return SourceOffer(provider=session.provider, cameras=tuple(session.cameras))

    async def discard(self, key: RelayKey) -> None:
        """Release an offered vendor session that no start will use."""
        offered = self._offers.pop(key, None)
        if offered is not None:
            await self._release(key, offered[1].channel or None)

    async def start(self, command: StartVendorSession) -> None:
        """Start the driver with the offered (or a fresh) vendor session; raises
        ``SourceError``."""
        session = self._take_offer(command.key) or await self._camera_session(command.key)
        driver = self.drivers.get(session.provider)
        if driver is None:
            raise _unsupported(session.provider)
        cameras = self._cameras(command.camera_ids, session)
        if not cameras:
            await self._release(command.key, session.channel or None)
            msg = "none of the requested cameras is offered by the mower"
            raise SourceUnavailableError(msg)
        source = RunningSource(
            key=command.key,
            session_id=command.session_id,
            relay_id=command.relay_id,
            provider=session.provider,
            cameras=cameras,
            channel=session.channel or None,
            session=session,
        )
        try:
            await driver.start(source)
        except Exception as exc:
            await driver.stop(source)
            await self._release(command.key, source.channel)
            msg = f"the camera source did not start: {type(exc).__name__}"
            raise SourceUnavailableError(msg) from exc
        self.sources[command.relay_id] = source
        _log.info(
            "camera source started",
            mower_id=str(command.key.mower_id),
            provider=session.provider.value,
            cameras=list(cameras),
        )

    async def update_cameras(self, command: UpdateCameras) -> None:
        """Publish the new camera set."""
        source = self.sources.get(command.relay_id)
        if source is None:
            return
        driver = self.drivers[source.provider]
        await driver.update(source, tuple(command.camera_ids))

    async def stop(self, command: StopVendorSession) -> None:
        """Stop the driver and, with ``release``, release the vendor session."""
        source = self.sources.pop(command.relay_id, None)
        channel = None
        if source is not None:
            channel = source.channel
            await self.drivers[source.provider].stop(source)
        if command.release:
            await self._release(command.key, channel)

    # --- Reports ------------------------------------------------------------------------------

    def report_failure(self, source: RunningSource) -> None:
        """A driver's process failed for good: report it outside the caller's stack."""
        if self.sources.get(source.relay_id) is not source:
            return
        task = asyncio.create_task(
            self._reports().source_failed(source.key, source.session_id),
            name="camera-relay-source-failed",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def mark_started(self, source: RunningSource, publishing: list[str]) -> None:
        """The reconciler saw media of ``publishing``: report it once."""
        if source.started_reported:
            return
        source.started_reported = True
        source.published.update(publishing)
        await self._reports().source_started(
            source.key, source.session_id, source.provider, publishing
        )

    async def mark_published(self, source: RunningSource, cameras: list[str]) -> None:
        """The reconciler saw more cameras of a started source deliver: report them once."""
        new = [camera for camera in cameras if camera not in source.published]
        if not new or not source.started_reported:
            return
        source.published.update(new)
        await self._reports().cameras_published(source.key, source.session_id, new)

    # --- Gateway requests ---------------------------------------------------------------------

    async def camera_session(self, key: RelayKey) -> CameraSession:
        """A fresh vendor session for ``key`` (the drivers' renewals); raises ``SourceError``."""
        return await self._camera_session(key)

    def _take_offer(self, key: RelayKey) -> CameraSession | None:
        offered = self._offers.pop(key, None)
        if offered is None:
            return None
        fetched_at, session = offered
        if self._monotonic() - fetched_at > OFFER_REUSE_SECONDS:
            return None
        return session

    @staticmethod
    def _cameras(requested: Sequence[str], session: CameraSession) -> tuple[str, ...]:
        offered = {camera.id for camera in session.cameras}
        if not offered:
            return tuple(requested)
        return tuple(camera for camera in requested if camera in offered)

    async def _camera_session(self, key: RelayKey) -> CameraSession:
        bus = self._bus()
        if bus is None or not bus.is_connected:
            msg = "the event bus is not connected"
            raise SourceUnavailableError(msg)
        request = CameraSessionRequest(
            mower_id=key.mower_id, account_id=key.account_id, requested_by=self.principal
        )
        try:
            message = await bus.request(
                camera_session_subject(key.mower_id), request, self.timeout_seconds
            )
            reply = message.model(CameraSessionReply)
        except BusError as exc:
            msg = f"the gateway did not answer the camera session request: {type(exc).__name__}"
            raise SourceUnavailableError(msg) from exc
        except ValueError as exc:
            msg = "the gateway's camera session reply is invalid"
            raise SourceUnavailableError(msg) from exc
        if reply.error is not None:
            raise _session_error(reply.error.code, reply.error.detail)
        assert reply.session is not None  # noqa: S101 - exactly one of session or error
        return reply.session

    async def _release(self, key: RelayKey, channel: str | None) -> None:
        bus = self._bus()
        if bus is None or not bus.is_connected:
            _log.warning("camera stream not released: no bus", mower_id=str(key.mower_id))
            return
        request = CameraReleaseRequest(
            mower_id=key.mower_id,
            account_id=key.account_id,
            requested_by=self.principal,
            channel=channel,
        )
        try:
            message = await bus.request(
                camera_release_subject(key.mower_id), request, self.timeout_seconds
            )
            reply = message.model(CameraReleaseReply)
        except (BusError, ValueError) as exc:
            _log.warning(
                "camera stream not released",
                mower_id=str(key.mower_id),
                error=type(exc).__name__,
            )
            return
        _log.info(
            "camera stream released",
            mower_id=str(key.mower_id),
            released=reply.released,
            error=reply.error.code.value if reply.error else None,
        )


def _unsupported(provider: CameraProvider) -> UnsupportedProviderError:
    return UnsupportedProviderError(
        f"the relay has no available source driver for camera provider {provider.value} (ADR 0045)"
    )


def _session_error(code: CameraSessionErrorCode, detail: str) -> SourceError:
    text = f"the gateway refused the camera session ({code.value}): {detail}"[:500]
    if code is CameraSessionErrorCode.FORBIDDEN:
        return ForbiddenSourceError(text)
    return SourceUnavailableError(text)

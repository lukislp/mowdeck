"""The Device-Gateway use cases: the volatile mower registry and command admission."""

import asyncio
import base64
import contextlib
from collections.abc import Iterable
from typing import Literal
from uuid import UUID

from mowdeck_contracts.v1 import (
    CameraSessionError,
    CameraSessionErrorCode,
    CameraSessionReply,
    CameraSessionRequest,
    CommandEnvelope,
    CommandResult,
    CommandStatus,
    MowerRegistration,
    PathPreviewError,
    PathPreviewErrorCode,
    PathPreviewGatewayRequest,
    PathPreviewReply,
    PhotoError,
    PhotoErrorCode,
    PhotoReply,
    PhotoRequest,
    utc_now,
)
from mowdeck_control import CommandRejected, NotConnected, PathUnavailable
from mowdeck_service_kit import get_logger

from device_gateway.application.ports import (
    CameraSnapshotter,
    Clock,
    ConnectionProvider,
    CredentialSource,
    FleetDirectory,
    GatewayPublisher,
    SnapshotFailed,
)
from device_gateway.application.session import (
    MowerSession,
    PathPreviewUnsupported,
    PhotoUnsupported,
    SessionTiming,
)
from device_gateway.domain.rules import (
    Shard,
    backoff,
    camera_rejection,
    command_rejection,
    preview_rejection,
)

_log = get_logger(__name__)


class GatewayDraining(Exception):  # noqa: N818 - reads as a state
    """The gateway is shutting down and accepts no new commands (the message is redelivered)."""


class DeviceGateway:
    """Holds the mowers of one shard, admits commands and hands them to their sessions.

    The registry is volatile: it is rebuilt on every start from Fleet (``fleet.mowers.list``),
    the Fleet events and, in development, the bootstrap list.
    """

    def __init__(
        self,
        *,
        shard: Shard,
        provider: ConnectionProvider,
        publisher: GatewayPublisher,
        timing: SessionTiming,
        clock: Clock = utc_now,
        credentials: CredentialSource | None = None,
        snapshotter: CameraSnapshotter | None = None,
        snapshot_timeout_seconds: float = 20.0,
    ) -> None:
        self.shard = shard
        self._snapshotter = snapshotter
        self._snapshot_timeout_seconds = snapshot_timeout_seconds
        self._credentials = credentials
        self._warned_sharded_cloud = False
        self._provider = provider
        self._publisher = publisher
        self._timing = timing
        self._clock = clock
        self._sessions: dict[UUID, MowerSession] = {}
        self._accepting = True
        self._fleet_sync: asyncio.Task[None] | None = None
        self.fleet_synced = False
        """True once Fleet answered ``fleet.mowers.list``."""

    # --- registry -----------------------------------------------------------------------------

    @property
    def accepting(self) -> bool:
        """False once :meth:`stop_accepting` was called."""
        return self._accepting

    def sessions(self) -> list[MowerSession]:
        """The mowers held by this instance."""
        return list(self._sessions.values())

    def session(self, mower_id: UUID) -> MowerSession | None:
        """The session of ``mower_id``, if this instance holds it."""
        return self._sessions.get(mower_id)

    async def register(self, registration: MowerRegistration, *, source: str) -> bool:
        """Add or update a mower; returns False if it belongs to another shard."""
        mower_id = registration.mower_id
        if not self.shard.owns(mower_id):
            _log.debug("mower belongs to another shard", mower_id=str(mower_id), source=source)
            return False
        existing = self._sessions.get(mower_id)
        if existing is not None:
            old = existing.registration
            same_link = (old.connection_kind, old.connection_options) == (
                registration.connection_kind,
                registration.connection_options,
            )
            if same_link:
                existing.registration = registration
                _log.info("mower registration updated", mower_id=str(mower_id), source=source)
                return True
            await self.remove(mower_id, source=source)
        if (
            self.shard.count > 1
            and "linked_account_id" in registration.connection_options
            and not self._warned_sharded_cloud
        ):
            self._warned_sharded_cloud = True
            _log.warning(
                "linked Mammotion accounts are unsupported with several shards: mowers of one "
                "account may hold separate cloud sessions that log each other out (ADR 0034)",
                shard_count=self.shard.count,
            )
        session = MowerSession(
            registration,
            provider=self._provider,
            publisher=self._publisher,
            timing=self._timing,
            clock=self._clock,
            credentials=self._credentials,
            snapshotter=self._snapshotter,
            snapshot_timeout_seconds=self._snapshot_timeout_seconds,
        )
        self._sessions[mower_id] = session
        session.start()
        _log.info(
            "mower registered",
            mower_id=str(mower_id),
            kind=str(registration.connection_kind),
            source=source,
        )
        return True

    async def register_all(self, registrations: Iterable[MowerRegistration], *, source: str) -> int:
        """Register every mower of this shard from ``registrations``; returns how many."""
        count = 0
        for registration in registrations:
            if await self.register(registration, source=source):
                count += 1
        return count

    async def remove(self, mower_id: UUID, *, source: str) -> bool:
        """Close and forget a mower; returns False if it was not held here."""
        session = self._sessions.pop(mower_id, None)
        if session is None:
            return False
        await session.close()
        _log.info("mower removed", mower_id=str(mower_id), source=source)
        return True

    # --- Fleet --------------------------------------------------------------------------------

    def start_fleet_sync(self, fleet: FleetDirectory, *, retry_max_seconds: float) -> None:
        """Load the shard's mowers from Fleet in the background, retrying with backoff."""
        if self._fleet_sync is None:
            self._fleet_sync = asyncio.create_task(
                self._sync_from_fleet(fleet, retry_max_seconds), name="fleet-sync"
            )

    async def _sync_from_fleet(self, fleet: FleetDirectory, retry_max_seconds: float) -> None:
        for attempt, delay in enumerate(backoff(1.0, max(1.0, retry_max_seconds)), start=1):
            try:
                registrations = await fleet.list_mowers(self.shard)
            except Exception as exc:
                _log.warning(
                    "fleet did not answer fleet.mowers.list, running with the known mowers",
                    attempt=attempt,
                    retry_in_seconds=delay,
                    error=repr(exc),
                )
                await asyncio.sleep(delay)
                continue
            count = await self.register_all(registrations, source="fleet")
            self.fleet_synced = True
            _log.info("mowers loaded from fleet", count=count)
            return

    # --- commands -----------------------------------------------------------------------------

    async def submit(self, envelope: CommandEnvelope) -> asyncio.Task[CommandResult] | None:
        """Admit a command and start executing it; None if it was ignored or rejected.

        Commands for mowers of other shards are ignored (their owner answers them). Commands for
        unknown mowers or from an account that does not own the mower are rejected here and
        never reach the controller. Raises :class:`GatewayDraining` while shutting down.
        """
        if not self._accepting:
            raise GatewayDraining("the device gateway is shutting down")
        if not self.shard.owns(envelope.mower_id):
            _log.debug("command for another shard ignored", command_id=str(envelope.command_id))
            return None
        session = self._sessions.get(envelope.mower_id)
        reason = command_rejection(session.registration if session else None, envelope)
        if session is None or reason is not None:
            _log.warning(
                "command rejected",
                command_id=str(envelope.command_id),
                mower_id=str(envelope.mower_id),
                account_id=str(envelope.account_id),
                reason=reason,
            )
            await self._publisher.command_result(
                CommandResult(
                    command_id=envelope.command_id,
                    mower_id=envelope.mower_id,
                    status=CommandStatus.REJECTED,
                    reason=reason,
                    at=self._clock(),
                    correlation_id=envelope.correlation_id,
                )
            )
            return None
        return session.submit(envelope)

    # --- camera -------------------------------------------------------------------------------

    async def camera_session(self, request: CameraSessionRequest) -> CameraSessionReply | None:
        """Answer a camera session request; None when the mower belongs to another shard.

        The account of the request must own the mower (like commands). The session holds a
        secret token: it is returned, never logged.
        """
        if not self.shard.owns(request.mower_id):
            return None
        session = self._sessions.get(request.mower_id)
        refusal = camera_rejection(session.registration if session else None, request)
        if refusal is not None or session is None:
            code, detail = refusal or (CameraSessionErrorCode.NOT_FOUND, "unknown mower")
            _log.warning(
                "camera session refused",
                mower_id=str(request.mower_id),
                account_id=str(request.account_id),
                code=code.value,
            )
            return _camera_error(code, detail)
        try:
            camera = await session.camera_session()
        except NotConnected as exc:
            return _camera_error(CameraSessionErrorCode.UNAVAILABLE, str(exc))
        except Exception as exc:
            _log.warning(
                "camera session failed", mower_id=str(request.mower_id), error=type(exc).__name__
            )
            return _camera_error(CameraSessionErrorCode.UNAVAILABLE, "the camera did not answer")
        if camera is None:
            return _camera_error(
                CameraSessionErrorCode.UNSUPPORTED, "this mower has no camera on its connection"
            )
        return CameraSessionReply(session=camera)

    # --- mow path preview ---------------------------------------------------------------------

    async def path_preview(
        self, request: PathPreviewGatewayRequest, *, timeout_seconds: float = 75.0
    ) -> PathPreviewReply | None:
        """Answer a path preview request; None when the mower belongs to another shard.

        The account of the request must own the mower (like commands). The mower plans the
        route of the areas and pattern without starting; the wait ends after
        ``timeout_seconds`` with ``timeout``.
        """
        if not self.shard.owns(request.mower_id):
            return None
        session = self._sessions.get(request.mower_id)
        refusal = preview_rejection(session.registration if session else None, request)
        if refusal is not None or session is None:
            code, detail = refusal or (PathPreviewErrorCode.NOT_FOUND, "unknown mower")
            _log.warning(
                "path preview refused",
                mower_id=str(request.mower_id),
                account_id=str(request.account_id),
                reason=detail,
            )
            return _preview_error(code, detail)
        try:
            async with asyncio.timeout(timeout_seconds):
                path = await session.preview_path(request.area_ids, request.pattern)
        except NotConnected as exc:
            return _preview_error(PathPreviewErrorCode.UNAVAILABLE, str(exc))
        except PathPreviewUnsupported as exc:
            return _preview_error(PathPreviewErrorCode.UNSUPPORTED, str(exc))
        except CommandRejected as exc:
            return _preview_error(PathPreviewErrorCode.REJECTED, exc.reason)
        except PathUnavailable as exc:
            code = PathPreviewErrorCode.TIMEOUT if exc.timed_out else PathPreviewErrorCode.FAILED
            return _preview_error(code, exc.reason)
        except TimeoutError:
            return _preview_error(
                PathPreviewErrorCode.TIMEOUT, "the mower did not hand back its route in time"
            )
        except Exception as exc:
            _log.warning("path preview failed", mower_id=str(request.mower_id), error=repr(exc))
            return _preview_error(
                PathPreviewErrorCode.FAILED, f"planning the route failed: {type(exc).__name__}"
            )
        _log.info(
            "path preview answered",
            mower_id=str(request.mower_id),
            lines=len(path.polylines),
            points=path.points_total,
        )
        return PathPreviewReply(path=path)

    # --- photos -------------------------------------------------------------------------------

    async def photo(
        self, request: PhotoRequest, *, timeout_seconds: float = 5.0
    ) -> PhotoReply | None:
        """Answer a photo request (Vision, ADR 0033); None when this instance does not hold the
        mower (another instance answers, so this one stays silent).

        A mower of another account is answered with ``not_found``, exactly like an unknown one.
        The image is capped at ``request.max_bytes``. Images are never logged.
        """
        session = self._sessions.get(request.mower_id)
        if session is None:
            return None
        if session.registration.account_id != request.account_id:
            _log.warning(
                "photo request of another account refused",
                mower_id=str(request.mower_id),
                account_id=str(request.account_id),
            )
            return _photo_error(PhotoErrorCode.NOT_FOUND, "mower not found")
        try:
            async with asyncio.timeout(timeout_seconds):
                photo = await session.capture_photo()
        except NotConnected:
            return _photo_error(PhotoErrorCode.NOT_CONNECTED, "the mower is not connected")
        except PhotoUnsupported as exc:
            return _photo_error(PhotoErrorCode.UNSUPPORTED, str(exc) or "no photos")
        except SnapshotFailed as exc:
            _log.info("camera frame failed", mower_id=str(request.mower_id), detail=str(exc))
            return _photo_error(PhotoErrorCode.FAILED, str(exc)[:500])
        except TimeoutError:
            return _photo_error(PhotoErrorCode.FAILED, "taking the photo timed out")
        except Exception as exc:
            _log.warning("photo failed", mower_id=str(request.mower_id), error=repr(exc))
            return _photo_error(
                PhotoErrorCode.FAILED, f"taking the photo failed: {type(exc).__name__}"
            )
        if len(photo.data) > request.max_bytes:
            return _photo_error(
                PhotoErrorCode.TOO_LARGE,
                f"the image has {len(photo.data)} bytes, more than allowed",
            )
        media_type: Literal["image/jpeg", "image/png"] = (
            "image/png" if photo.media_type == "image/png" else "image/jpeg"
        )
        _log.info("photo taken", mower_id=str(request.mower_id), bytes=len(photo.data))
        return PhotoReply(
            image_base64=base64.b64encode(photo.data).decode("ascii"),
            media_type=media_type,
            taken_at=self._clock(),
        )

    # --- shutdown -----------------------------------------------------------------------------

    def stop_accepting(self) -> None:
        """Refuse new commands from now on."""
        self._accepting = False

    async def drain(self) -> None:
        """Wait until every admitted command has published its result."""
        await asyncio.gather(*(s.wait_idle() for s in self.sessions()))

    async def close(self) -> None:
        """Stop the Fleet sync, answer in-flight commands and close every connection."""
        self.stop_accepting()
        if self._fleet_sync is not None:
            self._fleet_sync.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._fleet_sync
            self._fleet_sync = None
        await self.drain()
        sessions, self._sessions = list(self._sessions.values()), {}
        await asyncio.gather(*(s.close() for s in sessions))
        _log.info("device gateway closed", mowers=len(sessions))


def _camera_error(code: CameraSessionErrorCode, detail: str) -> CameraSessionReply:
    return CameraSessionReply(error=CameraSessionError(code=code, detail=detail[:500]))


def _preview_error(code: PathPreviewErrorCode, detail: str) -> PathPreviewReply:
    return PathPreviewReply(error=PathPreviewError(code=code, detail=detail[:500]))


def _photo_error(code: PhotoErrorCode, detail: str) -> PhotoReply:
    return PhotoReply(error=PhotoError(code=code, detail=detail))

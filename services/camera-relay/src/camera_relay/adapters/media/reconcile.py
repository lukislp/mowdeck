"""The reconcile loop: what the media server sees, fed to the use cases every 2 s.

One pass (:meth:`Reconciler.run_once`):

1. **Source readiness.** A started source whose camera paths are ready on the media server is
   reported once as ``source_started`` with the ready cameras (``STARTING -> LIVE``); cameras
   that become ready later are reported once each (``cameras_published``), so replies say
   which cameras deliver (``RelayTile.available``).
2. **Readers.** Every WHEP reader is looked up in the bindings the auth callback made:
   * bound and its ``bytesSent`` grew since the last pass: ``media_confirmed`` (once per
     binding, again after a loss); a lease that no longer exists gets its reader kicked;
   * bound but ``bytesSent`` stalled for ``stall_polls`` passes: ``media_lost`` (the lease may
     reconnect until its binding deadline; a short source freeze shorter than that keeps it);
   * not bound (the relay restarted, or the binding was dropped): kicked.
3. **Gone readers.** A binding whose reader disappeared: ``media_lost`` and the binding is
   dropped.

Kicks of closed leases (``KickViewer``) go through :class:`~camera_relay.adapters.media.
mediamtx.MediaMtxPlane` when the use cases execute the effect, not here.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from mowdeck_service_kit import get_logger

from camera_relay.adapters.gateway import GatewayVendorSessions
from camera_relay.adapters.media.mediamtx import MediaMtxApi, MediaServerError
from camera_relay.adapters.media.pipeline import viewer_path
from camera_relay.application.media_auth import VIEWER_PATH, WhepBindings

_log = get_logger("camera_relay.media.reconcile")


class MediaReports(Protocol):
    """Where readers are reported (``RelayService``)."""

    async def media_confirmed(self, lease_id: UUID, whep_session: str, camera: str = "") -> bool:
        """A viewer's reader of ``camera`` receives media; False when the lease is gone."""
        ...

    async def media_lost(self, lease_id: UUID, whep_session: str | None = None) -> bool:
        """The viewer's reader ``whep_session`` ended or stalled."""
        ...


@dataclass(slots=True)
class _Reader:
    lease_id: UUID
    bytes_sent: int = 0
    stalled: int = 0
    missing: int = 0
    confirmed: bool = False


class Reconciler:
    """Compares the media server's readers and paths with the relay's state."""

    def __init__(
        self,
        *,
        api: MediaMtxApi,
        bindings: WhepBindings,
        vendor: GatewayVendorSessions,
        reports: Callable[[], MediaReports],
        stall_polls: int = 3,
    ) -> None:
        self.api = api
        self.bindings = bindings
        self.vendor = vendor
        self._reports = reports
        self.stall_polls = stall_polls
        self._readers: dict[str, _Reader] = {}

    async def run(self, interval: float) -> None:
        """Run a pass every ``interval`` seconds until cancelled."""
        while True:
            try:
                await self.run_once()
            except MediaServerError as exc:
                _log.warning("media server not reachable", error=str(exc))
            except Exception:
                _log.exception("reconcile pass failed")
            await asyncio.sleep(interval)

    async def run_once(self) -> None:
        """One pass: source readiness, then readers."""
        await self._sources()
        await self._viewers()

    async def _sources(self) -> None:
        pending = [
            s
            for s in self.vendor.sources.values()
            if not s.started_reported or any(c not in s.published for c in s.cameras)
        ]
        if not pending:
            return
        ready = {path.name for path in await self.api.paths() if path.ready}
        for source in pending:
            publishing = [c for c in source.cameras if viewer_path(source.relay_id, c) in ready]
            if not publishing:
                continue
            if source.started_reported:
                await self.vendor.mark_published(source, publishing)
            else:
                await self.vendor.mark_started(source, publishing)

    async def _viewers(self) -> None:
        sessions = await self.api.webrtc_sessions()
        reports = self._reports()
        seen: set[str] = set()
        for session in sessions:
            match = VIEWER_PATH.match(session.path)
            if session.state != "read" or match is None:
                continue
            seen.add(session.id)
            lease_id = self.bindings.lease_of(session.id)
            if lease_id is None:
                _log.info("unbound reader kicked", whep_session=session.id)
                self._readers.pop(session.id, None)
                await self.api.kick_webrtc_session(session.id)
                continue
            reader = self._readers.get(session.id)
            if reader is None or reader.lease_id != lease_id:
                reader = _Reader(lease_id)
                self._readers[session.id] = reader
            reader.missing = 0
            growing = session.bytes_sent > reader.bytes_sent
            reader.bytes_sent = session.bytes_sent
            if growing:
                reader.stalled = 0
                if not reader.confirmed:
                    reader.confirmed = True
                    _log.info(
                        "viewer media confirmed", whep_session=session.id, lease_id=str(lease_id)
                    )
                    if not await reports.media_confirmed(lease_id, session.id, match["camera"]):
                        self._forget(session.id)
                        await self.api.kick_webrtc_session(session.id)
                continue
            reader.stalled += 1
            if reader.confirmed and reader.stalled >= self.stall_polls:
                reader.confirmed = False
                _log.info("viewer reader stalled", whep_session=session.id)
                await reports.media_lost(lease_id, session.id)
        for session_id, lease_id in self.bindings.sessions().items():
            if session_id in seen:
                continue
            reader = self._readers.setdefault(session_id, _Reader(lease_id))
            reader.missing += 1
            # Bound by the auth callback but not listed yet: one pass of grace.
            if reader.missing < 2 and not reader.confirmed:
                continue
            self._forget(session_id)
            if reader.confirmed:
                _log.info("viewer reader gone", whep_session=session_id, lease_id=str(lease_id))
                await reports.media_lost(lease_id, session_id)

    def _forget(self, session_id: str) -> None:
        self._readers.pop(session_id, None)
        self.bindings.unbind(session_id)

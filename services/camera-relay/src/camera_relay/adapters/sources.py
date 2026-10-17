"""Source and media plane adapters available before the media plane exists.

ADR 0045 builds the relay in steps. Until the media plane (step 4) and the source drivers (the
``fake`` source in step 4, other providers in step 7) exist, the relay has no way to start a
source: :class:`NoSourceDriver` answers every start with ``unavailable``, explicitly, so a
client sees a clear error and never a lease that cannot play. :class:`NoMediaPlane` has no
readers to end.
"""

from mowdeck_service_kit import get_logger

from camera_relay.application.ports import SourceOffer, SourceUnavailableError
from camera_relay.domain.effects import StartVendorSession, StopVendorSession, UpdateCameras
from camera_relay.domain.models import RelayKey

_log = get_logger("camera_relay.sources")

NO_DRIVER_DETAIL = (
    "no camera source driver is installed yet (camera relay: media plane and source drivers "
    "follow in later steps of ADR 0045)"
)


class NoSourceDriver:
    """The ``VendorSessions`` port without any driver: every offer and start is
    ``unavailable`` (a relay without a media plane is misconfigured, not a missing driver)."""

    async def offer(self, key: RelayKey) -> SourceOffer:
        """Refuse: no driver can start a source."""
        del key
        raise SourceUnavailableError(NO_DRIVER_DETAIL)

    async def discard(self, key: RelayKey) -> None:
        """Nothing was offered."""
        del key

    async def start(self, command: StartVendorSession) -> None:
        """Refuse: no driver can start a source yet."""
        raise SourceUnavailableError(NO_DRIVER_DETAIL)

    async def update_cameras(self, command: UpdateCameras) -> None:
        """Nothing runs, nothing to update."""
        del command

    async def stop(self, command: StopVendorSession) -> None:
        """Nothing runs, nothing to stop."""
        del command


class NoMediaPlane:
    """The ``MediaPlane`` port without a media server: no readers exist to end."""

    async def kick(self, whep_session: str) -> None:
        """Nothing to end."""
        _log.debug("no media plane: kick ignored", whep_session=whep_session)

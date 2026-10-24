"""Why the mower delivered no camera photo, judged from its state and the gateway's answer.

Pure, no I/O. Live case 2026-10-24: a LUBA stopped with error 1201 at the far end of the garden.
Its Wi-Fi signal had fallen from -30 dBm at the dock to -79 dBm (it stays on a far access point
instead of roaming to a nearer one), its cameras stopped publishing 16 s before the error, and
every photo request after it ended with the camera relay's ``frame-timeout``. MowDeck cannot fix
the radio link; it can retry within a bounded time and say clearly why there is no photo.

The thresholds are rules of thumb for a camera stream (two H.265 streams of about 1 Mbit/s each):
below about -70 dBm Wi-Fi or -105 dBm on the mobile network (LTE RSRP-like values as the vendor
reports them) a stream that worked at the dock usually stalls.
"""

from dataclasses import dataclass
from typing import Final

from mowdeck_contracts.v1 import MowerActivity, MowerState, NetworkKind, VisionPhotoProblem

WEAK_WIFI_DBM: Final = -70
"""At or below this Wi-Fi signal (dBm) a camera stream is likely to stall."""
WEAK_MOBILE_DBM: Final = -105
"""At or below this mobile signal (dBm) a camera stream is likely to stall."""

_OFFLINE_CODES: Final = frozenset({"not_connected", "not_found"})


@dataclass(frozen=True, slots=True)
class PhotoDiagnosis:
    """The most likely reason for a missing photo."""

    problem: VisionPhotoProblem
    signal_dbm: int | None = None
    gateway: str | None = None
    """What the gateway answered (``timeout`` or ``<code>: <detail>``), for the log and detail."""

    def text(self, attempts: int = 1) -> str:
        """The English detail of the proposal (the app words it from ``problem``)."""
        tries = f" after {attempts} attempts" if attempts > 1 else ""
        match self.problem:
            case VisionPhotoProblem.WEAK_WIFI:
                why = f"weak Wi-Fi ({self.signal_dbm} dBm), the camera stream did not get through"
            case VisionPhotoProblem.WEAK_MOBILE:
                why = (
                    f"weak mobile signal ({self.signal_dbm} dBm), the camera stream did not get "
                    "through"
                )
            case VisionPhotoProblem.OFFLINE:
                why = "the mower is offline"
            case _:
                why = "the camera delivered no frame in time"
        hint = "; start the analysis by hand once the connection is better"
        return f"no photo{tries}: {why}{hint}"[:500]


def diagnose(state: MowerState | None, gateway: str | None = None) -> PhotoDiagnosis:
    """The likely reason from the newest state (``None``: unknown) and the gateway's answer
    (``timeout`` or ``<code>: <detail>``; a ``not_connected`` answer means offline). A mower
    without an internet uplink of its own (only Bluetooth, or none) counts as offline: the camera
    needs the internet."""
    code = gateway.split(":", 1)[0].strip() if gateway else None
    if state is None or state.activity is MowerActivity.OFFLINE or code in _OFFLINE_CODES:
        return PhotoDiagnosis(VisionPhotoProblem.OFFLINE, gateway=gateway)
    signal = state.signal
    if signal is not None and signal.network in (NetworkKind.BLE, NetworkKind.NONE):
        return PhotoDiagnosis(VisionPhotoProblem.OFFLINE, gateway=gateway)
    if signal is not None:
        wifi, mobile = signal.wifi_dbm, signal.mobile_dbm
        on_mobile = signal.network is NetworkKind.MOBILE
        if not on_mobile and wifi is not None and wifi <= WEAK_WIFI_DBM:
            return PhotoDiagnosis(VisionPhotoProblem.WEAK_WIFI, wifi, gateway)
        if on_mobile and mobile is not None and mobile <= WEAK_MOBILE_DBM:
            return PhotoDiagnosis(VisionPhotoProblem.WEAK_MOBILE, mobile, gateway)
    return PhotoDiagnosis(VisionPhotoProblem.NO_FRAME, gateway=gateway)


__all__ = ["WEAK_MOBILE_DBM", "WEAK_WIFI_DBM", "PhotoDiagnosis", "diagnose"]

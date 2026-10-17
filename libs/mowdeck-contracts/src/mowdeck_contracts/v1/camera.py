"""Live camera sessions (ADR 0032): short-lived credentials for the browser to join the stream.

The video of a Mammotion mower goes through Agora's cloud. MowDeck never touches the media: the
browser joins the Agora channel directly with the Agora Web SDK, using credentials the adapter
fetched from the Mammotion cloud. The path of one request:

1. The web app calls ``POST /api/v1/fleet/mowers/{id}/camera/session``. Fleet checks ``can_view``
   on the mower (404 semantics) and the plan feature ``camera``, writes an audit row and sends a
   :class:`CameraSessionRequest` on :func:`~mowdeck_contracts.v1.subjects.camera_session_subject`
   (core NATS request/reply, internal).
2. The Device-Gateway instance that holds the mower checks the account like for commands, asks
   the connection for a session and answers with a :class:`CameraSessionReply`.
3. The browser joins the channel as audience (``provider`` ``agora``) or renders a generated test
   pattern (``provider`` ``fake``, the simulator).

With the camera relay (ADR 0045) the browser no longer joins the vendor channel: the session
has ``provider`` ``relay`` and carries a :class:`RelayEndpoint` (WHEP URLs per camera and a
short-lived ticket) instead of vendor credentials. The relay's own request/reply contracts are
in :mod:`mowdeck_contracts.v1.relay`.

``token``, the encryption ``key`` and the relay ``ticket`` are secrets: they are excluded from
``repr`` and must never be logged. Use :meth:`CameraSession.redacted` for log lines and scripts.
"""

from enum import StrEnum
from typing import Any, Final, Self
from uuid import UUID

from pydantic import Field, model_validator

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime
from mowdeck_contracts.v1.principal import PrincipalRef

CAMERA_FEATURE: Final = "camera"
"""Plan feature of the live camera (ADR 0028, ADR 0032)."""

REDACTED: Final = "<redacted>"


class CameraProvider(StrEnum):
    """Who delivers the video."""

    AGORA = "agora"
    """The browser joins an Agora RTC channel with the Agora Web SDK (Mammotion cloud)."""
    FAKE = "fake"
    """No video: the web app renders a generated test pattern (the simulator)."""
    RELAY = "relay"
    """The client plays MowDeck's camera relay over WebRTC (WHEP, ADR 0045); the session carries
    a :class:`RelayEndpoint` and no vendor credentials."""


class CameraTransport(StrEnum):
    """How a client wants to reach the camera (ADR 0045). Chosen per browser; Fleet decides."""

    AUTO = "auto"
    """The relay; the direct vendor path only when the relay answers ``unsupported_provider``.
    Never switched while a drive session runs."""
    RELAY = "relay"
    """Only the relay; its errors are shown, never a fallback."""
    DIRECT = "direct"
    """The vendor session as before (ADR 0032), a manual per-browser option."""


class CameraPlacement(StrEnum):
    """Where a camera sits on the mower. Clients pair ``front_left`` and ``front_right`` for the
    side-by-side view and stereo frames (both front cameras of a LUBA 2 AWD X)."""

    FRONT_LEFT = "front_left"
    FRONT_RIGHT = "front_right"
    FRONT = "front"
    REAR = "rear"


class CameraInfo(ContractModel):
    """One camera of the mower."""

    id: str = Field(min_length=1, max_length=64, description="Camera id as the vendor names it.")
    name: str = Field(max_length=100, description="A short display name, e.g. `Front left`.")
    uid: int | None = Field(
        default=None,
        ge=0,
        le=2**32 - 1,
        description=(
            "Agora uid the mower publishes this camera under, if known. Clients fall back to the "
            "first remote user that publishes video."
        ),
    )
    placement: CameraPlacement | None = Field(
        default=None,
        description=(
            "Where the camera sits, if known. A `front_left` and a `front_right` camera form a "
            "stereo pair the client may show side by side."
        ),
    )


class CameraEncryption(ContractModel):
    """Channel encryption the client must configure before joining (Agora
    ``setEncryptionConfig``)."""

    mode: str = Field(
        max_length=32, description="Agora encryption mode, for Mammotion `aes-256-gcm2`."
    )
    key: str = Field(
        repr=False, min_length=1, max_length=256, description="The secret, used verbatim."
    )
    salt: str | None = Field(
        default=None,
        repr=False,
        max_length=256,
        description="Base64 salt (32 bytes decoded) for the `gcm2` modes; null when not sent.",
    )


class CameraQuota(ContractModel):
    """The vendor's video budget (Mammotion meters video over 4G only)."""

    used_minutes: float = Field(ge=0)
    total_minutes: float = Field(ge=0)


RELAY_HEARTBEAT_SECONDS: Final = 15
"""Default interval of a viewer's lease heartbeat (``relay.camera.renew``)."""


class RelayTile(ContractModel):
    """One camera as the relay serves it: a WHEP endpoint per camera."""

    camera_id: str = Field(
        min_length=1, max_length=64, description="The `CameraInfo.id` this endpoint shows."
    )
    whep_url: str = Field(
        min_length=2,
        max_length=512,
        pattern=r"^(/|https?://)[^\s]*$",
        description=(
            "WHEP endpoint of this camera: an absolute path on the MowDeck origin or an "
            "`http(s)` URL. The path carries a random session token, never the mower id."
        ),
    )


class RelayEndpoint(ContractModel):
    """Where and with what a client plays a relay session (``provider`` ``relay``)."""

    tiles: list[RelayTile] = Field(min_length=1, max_length=8)
    ticket: str = Field(
        repr=False,
        min_length=1,
        max_length=2048,
        description=(
            "Short-lived ticket signed by the relay, bound to account, mower, lease and paths. "
            "Sent as the WHEP bearer token. Never log it."
        ),
    )
    lease_id: UUID = Field(description="The viewer's lease; renew and detach name it.")
    heartbeat_seconds: int = Field(
        default=RELAY_HEARTBEAT_SECONDS,
        ge=5,
        le=60,
        description="How often the client renews the lease while the page is visible.",
    )

    @model_validator(mode="after")
    def _unique_cameras(self) -> Self:
        ids = [tile.camera_id for tile in self.tiles]
        if len(ids) != len(set(ids)):
            msg = "each camera may appear in only one tile"
            raise ValueError(msg)
        return self


class CameraSession(ContractModel):
    """Credentials for one viewer to join the live video of one mower.

    Provider ``relay`` needs the ``relay`` endpoint and carries no vendor secrets (``token``
    and ``app_id`` empty, no ``encryption``); ``expires_at`` is then the ticket's expiry. Every
    other provider has no ``relay`` endpoint.
    """

    provider: CameraProvider
    app_id: str = Field(max_length=128, description="Agora app id (empty for `fake`).")
    channel: str = Field(max_length=64, description="Agora channel name (64 bytes at most).")
    token: str = Field(
        repr=False, max_length=2048, description="Short-lived Agora token. Never log it."
    )
    uid: int = Field(ge=0, le=2**32 - 1, description="The viewer's Agora uid (bound to the token).")
    cameras: list[CameraInfo] = Field(default_factory=list, max_length=8)
    encryption: CameraEncryption | None = Field(
        default=None, description="Set when the channel is encrypted."
    )
    expires_at: UtcDateTime = Field(
        description="When the token stops working; clients request a new session before."
    )
    quota: CameraQuota | None = Field(
        default=None, description="The video budget, when the vendor reports one."
    )
    relay: RelayEndpoint | None = Field(
        default=None, description="Set exactly when `provider` is `relay` (ADR 0045)."
    )

    @model_validator(mode="after")
    def _provider_rules(self) -> Self:
        if self.provider is CameraProvider.RELAY:
            if self.relay is None:
                msg = "provider relay needs a relay endpoint"
                raise ValueError(msg)
            if self.token or self.app_id or self.encryption is not None:
                msg = "provider relay carries no vendor token, app id or encryption"
                raise ValueError(msg)
        elif self.relay is not None:
            msg = f"provider {self.provider.value} has no relay endpoint"
            raise ValueError(msg)
        return self

    def redacted(self) -> dict[str, Any]:
        """A JSON-ready dump with ``token``, the encryption ``key`` and ``salt`` and the relay
        ``ticket`` replaced."""
        data = self.model_dump(mode="json")
        data["token"] = REDACTED if self.token else ""
        if data.get("encryption"):
            data["encryption"]["key"] = REDACTED
            if data["encryption"].get("salt"):
                data["encryption"]["salt"] = REDACTED
        if data.get("relay"):
            data["relay"]["ticket"] = REDACTED
        return data


class CameraSessionErrorCode(StrEnum):
    """Why no camera session was given."""

    NOT_FOUND = "not_found"
    """The gateway does not hold the mower (or the shard owner is down)."""
    UNSUPPORTED = "unsupported"
    """The mower or its connection has no camera."""
    UNAVAILABLE = "unavailable"
    """The mower is not connected or the vendor cloud did not answer."""
    FORBIDDEN = "forbidden"
    """The account of the request does not own the mower."""


class CameraSessionError(ContractModel):
    """The error of a :class:`CameraSessionReply`."""

    code: CameraSessionErrorCode
    detail: str = Field(max_length=500)


class CameraSessionRequest(ContractModel):
    """Request on ``gateway.mower.<id>.camera.session`` (internal, Fleet to Device-Gateway)."""

    mower_id: UUID
    account_id: UUID = Field(description="The mower's account as Fleet knows it.")
    requested_by: PrincipalRef


class CameraSessionReply(ContractModel):
    """Reply to :class:`CameraSessionRequest`: exactly one of ``session`` or ``error``."""

    session: CameraSession | None = None
    error: CameraSessionError | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.session is None) == (self.error is None):
            msg = "exactly one of session or error must be given"
            raise ValueError(msg)
        return self


class CameraReleaseRequest(ContractModel):
    """Request on ``gateway.mower.<id>.camera.release`` (internal, the camera relay to the
    Device-Gateway, ADR 0045): tell the mower to stop publishing its camera stream.

    Sent after the last viewer left a source session; the mower does not stop by itself.
    Idempotent: releasing a stream that no longer runs is harmless.
    """

    mower_id: UUID
    account_id: UUID = Field(description="The mower's account as the caller knows it.")
    requested_by: PrincipalRef
    channel: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "`CameraSession.channel` of the session to release, when known. Adapters that stop "
            "the stream per mower ignore it; null releases whatever the mower publishes."
        ),
    )


class CameraReleaseReply(ContractModel):
    """Reply to :class:`CameraReleaseRequest`.

    ``released`` true: the stop went out to the mower. ``released`` false without ``error``:
    the connection cannot release a stream (the adapter has no release port); nothing was
    sent and nothing is wrong. ``error`` (``not_found``, ``forbidden``, ``unavailable``): the
    request was refused or the stop could not be sent; ``released`` is then false.
    """

    released: bool = False
    error: CameraSessionError | None = None

    @model_validator(mode="after")
    def _error_is_not_released(self) -> Self:
        if self.error is not None and self.released:
            msg = "a reply with an error cannot be released"
            raise ValueError(msg)
        return self

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

``token`` and the encryption ``key`` are secrets: they are excluded from ``repr`` and must never
be logged. Use :meth:`CameraSession.redacted` for log lines and scripts.
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


class CameraSession(ContractModel):
    """Credentials for one viewer to join the live video of one mower."""

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

    def redacted(self) -> dict[str, Any]:
        """A JSON-ready dump with ``token``, the encryption ``key`` and ``salt`` replaced."""
        data = self.model_dump(mode="json")
        data["token"] = REDACTED if self.token else ""
        if data.get("encryption"):
            data["encryption"]["key"] = REDACTED
            if data["encryption"].get("salt"):
                data["encryption"]["salt"] = REDACTED
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

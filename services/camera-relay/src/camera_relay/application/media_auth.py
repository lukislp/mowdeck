"""Who may read and publish on the media server: the decisions of its HTTP auth callback.

MediaMTX asks the relay for every action (``authMethod: http``). The rules:

* ``read`` of a viewer path ``r/<relay_id>/<camera>`` over WebRTC (WHEP) only with a relay
  ticket (the WHEP bearer token) that is signed by this relay, not expired, names exactly this
  path, and whose account, mower, session (``relay_id``) and lease match a live lease of a
  running session. The reader's session id is bound to that lease, so the relay can confirm its
  media, notice when it is gone and end it when the lease closes.
* ``publish`` on a relay path (``r/<relay_id>/<camera>`` or ``.../src``), ``read`` over RTSP of
  a relay path (the transcoder reads ``.../src``) and ``api`` (the relay's own control API
  client) only with the internal publisher credential, compared in constant time.
* Everything else is denied: ``metrics``, ``playback``, ``pprof``, other paths, other
  protocols, readers without or with a foreign ticket.

Nothing here logs a ticket or a password.
"""

import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from camera_relay.application.ports import Clock, RelayRepository, TicketSigner
from camera_relay.domain.models import ACTIVE_STATES, RelayKey
from camera_relay.domain.tickets import InvalidTicketError

VIEWER_PATH = re.compile(r"^r/(?P<relay_id>[0-9a-f]{32})/(?P<camera>[A-Za-z0-9_-]{1,64})$")
"""A viewer path: ``r/<relay_id>/<camera>`` (the transcoded H.264 stream)."""
RELAY_PATH = re.compile(r"^r/(?P<relay_id>[0-9a-f]{32})/(?P<camera>[A-Za-z0-9_-]{1,64})(/src)?$")
"""Any path of the relay's pipeline: the viewer path or its source ``.../src``."""


class MediaAction(StrEnum):
    """Actions MediaMTX asks about."""

    PUBLISH = "publish"
    READ = "read"
    PLAYBACK = "playback"
    API = "api"
    METRICS = "metrics"
    PPROF = "pprof"


@dataclass(frozen=True, slots=True)
class MediaAuthRequest:
    """One question of the media server (the fields of its callback body that matter)."""

    action: str
    path: str = ""
    protocol: str = ""
    user: str = ""
    password: str = field(default="", repr=False)
    token: str = field(default="", repr=False)
    session_id: str | None = None
    """The reader's or publisher's session id on the media server."""


@dataclass(frozen=True, slots=True)
class MediaAuthDecision:
    """Allow or deny, with a reason for the log (never a secret)."""

    allowed: bool
    reason: str
    lease_id: UUID | None = None


def _deny(reason: str) -> MediaAuthDecision:
    return MediaAuthDecision(False, reason)


class WhepBindings:
    """Reader sessions of the media server bound to leases (in memory, one replica)."""

    def __init__(self) -> None:
        self._by_session: dict[str, UUID] = {}

    def bind(self, session_id: str, lease_id: UUID) -> None:
        """Remember that ``session_id`` reads for ``lease_id``."""
        self._by_session[session_id] = lease_id

    def lease_of(self, session_id: str) -> UUID | None:
        """The lease a reader session reads for."""
        return self._by_session.get(session_id)

    def unbind(self, session_id: str) -> None:
        """Forget a reader session."""
        self._by_session.pop(session_id, None)

    def sessions(self) -> dict[str, UUID]:
        """A copy of every binding."""
        return dict(self._by_session)


class MediaAuthorizer:
    """Answers the media server's auth callback."""

    def __init__(
        self,
        *,
        repository: RelayRepository,
        signer: TicketSigner,
        clock: Clock,
        bindings: WhepBindings,
        publisher_user: str,
        publisher_secret: Callable[[], str],
    ) -> None:
        self.repository = repository
        self.signer = signer
        self.clock = clock
        self.bindings = bindings
        self._user = publisher_user
        self._secret = publisher_secret

    def decide(self, request: MediaAuthRequest) -> MediaAuthDecision:
        """The decision for one request."""
        match request.action:
            case MediaAction.READ:
                if request.protocol == "webrtc":
                    return self._viewer_read(request)
                if request.protocol == "rtsp":
                    return self._internal(request, path_required=True)
                return _deny(f"read over {request.protocol or 'unknown'} is not offered")
            case MediaAction.PUBLISH:
                return self._internal(request, path_required=True)
            case MediaAction.API:
                return self._internal(request, path_required=False)
            case _:
                return _deny(f"action {request.action or 'unknown'} is not offered")

    def _internal(self, request: MediaAuthRequest, *, path_required: bool) -> MediaAuthDecision:
        if path_required and not RELAY_PATH.match(request.path):
            return _deny("not a relay path")
        if not request.user or not request.password:
            return _deny("no credentials")
        user_ok = hmac.compare_digest(request.user.encode(), self._user.encode())
        secret_ok = hmac.compare_digest(request.password.encode(), self._secret().encode())
        if not (user_ok and secret_ok):
            return _deny("wrong internal credentials")
        return MediaAuthDecision(True, f"internal {request.action}")

    def _viewer_read(self, request: MediaAuthRequest) -> MediaAuthDecision:
        match = VIEWER_PATH.match(request.path)
        if match is None:
            return _deny("not a viewer path")
        if not request.token:
            return _deny("no ticket")
        now = self.clock()
        try:
            claims = self.signer.verify(request.token, now)
        except InvalidTicketError as exc:
            return _deny(f"invalid ticket: {exc}")
        if match["relay_id"] != claims.relay_id:
            return _deny("ticket of another session")
        if not claims.allows(account_id=claims.account_id, path=request.path, now=now):
            return _deny("ticket does not cover the path")
        decision = self._live_lease(
            RelayKey(claims.account_id, claims.mower_id), claims.relay_id, claims.lease_id
        )
        if not decision.allowed:
            return decision
        if request.session_id:
            self.bindings.bind(request.session_id, claims.lease_id)
        return decision

    def _live_lease(self, key: RelayKey, relay_id: str, lease_id: UUID) -> MediaAuthDecision:
        session = self.repository.get(key)
        if session is None or session.relay_id != relay_id:
            return _deny("no running session for the ticket")
        if session.state not in ACTIVE_STATES:
            return _deny(f"session is {session.state.value}")
        lease = session.leases.get(lease_id)
        if lease is None or lease.key != key:
            return _deny("the ticket's lease is closed")
        return MediaAuthDecision(True, "ticket of a live lease", lease_id=lease_id)

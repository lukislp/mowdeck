"""In-memory repository of the relay sessions.

The relay runs as a single replica (ADR 0045): sessions, leases and the Vision minutes of the
day live in this process and are lost on a restart. Clients then get ``not_found`` on their next
renew and attach again; a source left running is stopped by the vendor session's own expiry.
"""

from collections.abc import Callable, Iterable
from uuid import UUID

from camera_relay.domain.models import RelayKey
from camera_relay.domain.policy import MowerStatus, RelayPolicy
from camera_relay.domain.session import RelaySession


class InMemoryRelayRepository:
    """Sessions per ``(account_id, mower_id)`` and the last known state per mower."""

    def __init__(
        self,
        *,
        policy: RelayPolicy | None = None,
        session_factory: Callable[[RelayKey, RelayPolicy, MowerStatus | None], RelaySession]
        | None = None,
    ) -> None:
        self.policy = policy or RelayPolicy()
        self._factory = session_factory or (
            lambda key, policy, status: RelaySession(key, policy=policy, status=status)
        )
        self._sessions: dict[RelayKey, RelaySession] = {}
        self._statuses: dict[UUID, MowerStatus] = {}

    def get(self, key: RelayKey) -> RelaySession | None:
        """The session of ``key``, if any."""
        return self._sessions.get(key)

    def get_or_create(self, key: RelayKey) -> RelaySession:
        """The session of ``key``; a new one starts with the mower's last known state."""
        session = self._sessions.get(key)
        if session is None:
            session = self._factory(key, self.policy, self._statuses.get(key.mower_id))
            self._sessions[key] = session
        return session

    def key_of_lease(self, lease_id: UUID) -> RelayKey | None:
        """The key whose session holds ``lease_id``."""
        for key, session in self._sessions.items():
            if lease_id in session.leases:
                return key
        return None

    def session_keys(self) -> list[RelayKey]:
        """Every key with a session (a copy: safe to change the repository while iterating)."""
        return list(self._sessions)

    def keys_of_mower(self, mower_id: UUID) -> Iterable[RelayKey]:
        """The keys of one mower (one per account that uses it)."""
        return [key for key in self._sessions if key.mower_id == mower_id]

    def set_status(self, mower_id: UUID, status: MowerStatus) -> None:
        """Remember the mower's state for new sessions."""
        self._statuses[mower_id] = status

    def remove(self, key: RelayKey) -> None:
        """Forget a dormant session."""
        self._sessions.pop(key, None)

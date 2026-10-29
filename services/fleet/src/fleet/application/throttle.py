"""Login throttle of linked accounts (ADR 0034): protects the vendor account from lockout.

Every create and re-read of a linked account is a login at a vendor cloud. A user (or a
stolen session) trying passwords would get the vendor account locked or the installation's
address banned, so Fleet limits failed logins per MowDeck account and vendor account
(provider and identity, ADR 0066):

* at most :attr:`LoginThrottle.max_failures` failed logins within :attr:`LoginThrottle.window`
  (default 5 in 15 minutes); further attempts answer 429 with ``Retry-After`` until the oldest
  failure leaves the window, without asking the vendor;
* the password of the last failure is remembered (an HMAC with a per-process random key, never
  the password) for the same window: trying the same wrong password again is refused at once,
  without asking the vendor and without counting;
* a successful login clears both.

The state lives in the memory of one Fleet replica (several replicas multiply the limit by
their number; the vendor's own limits still apply). Times come from the injected clock.
"""

import hashlib
import hmac
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from fleet.application.ports import Clock


class TooManyLoginAttempts(Exception):  # noqa: N818 - reads like the HTTP status it maps to
    """Too many failed logins for this e-mail; retry after ``retry_after_seconds``."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("too many failed logins for this vendor account; try again later")
        self.retry_after_seconds = retry_after_seconds


@dataclass(slots=True)
class _State:
    failures: deque[datetime] = field(default_factory=deque)
    last_wrong: tuple[bytes, datetime] | None = None


ThrottleKey = tuple[UUID, str]


class LoginThrottle:
    """Failed vendor logins per (MowDeck account, lower-case e-mail)."""

    def __init__(
        self,
        clock: Clock,
        *,
        max_failures: int = 5,
        window: timedelta = timedelta(minutes=15),
    ) -> None:
        self.clock = clock
        self.max_failures = max_failures
        self.window = window
        self._secret = os.urandom(32)
        self._states: dict[ThrottleKey, _State] = {}

    @staticmethod
    def key(account_id: UUID, email: str) -> ThrottleKey:
        """The throttle key of an e-mail of an account."""
        return account_id, email.strip().lower()

    def _digest(self, password: str) -> bytes:
        return hmac.new(self._secret, password.encode("utf-8"), hashlib.sha256).digest()

    def _state(self, key: ThrottleKey) -> _State:
        state = self._states.setdefault(key, _State())
        now = self.clock()
        while state.failures and now - state.failures[0] >= self.window:
            state.failures.popleft()
        if state.last_wrong is not None and now - state.last_wrong[1] >= self.window:
            state.last_wrong = None
        return state

    def check(self, key: ThrottleKey, password: str) -> bool:
        """Before a login: raise :class:`TooManyLoginAttempts` when the limit is reached;
        False when ``password`` is the remembered wrong one (refuse without a login)."""
        state = self._state(key)
        if len(state.failures) >= self.max_failures:
            retry = state.failures[0] + self.window - self.clock()
            raise TooManyLoginAttempts(max(1, int(retry.total_seconds()) + 1))
        wrong = state.last_wrong
        return wrong is None or not hmac.compare_digest(wrong[0], self._digest(password))

    def failed(self, key: ThrottleKey, password: str) -> None:
        """Record a refused login."""
        state = self._state(key)
        now = self.clock()
        state.failures.append(now)
        state.last_wrong = (self._digest(password), now)

    def succeeded(self, key: ThrottleKey) -> None:
        """Forget the failures of ``key``."""
        self._states.pop(key, None)

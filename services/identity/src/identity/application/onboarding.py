"""The setup tour of the web app (ADR 0062): the user's own choices, kept per user so the tour
follows them from the browser to the iPhone app.

* Only users and sub-users have a tour; API tokens and services get 403.
* Identity never decides whether a step is done (the web app reads that from the real state of
  Fleet, Scheduler, Notifications and Identity). It keeps the status of the tour, the skipped
  steps and the dismissed "first time here" hints, as keys the web app chooses; Identity only
  checks their shape and count, so a new step in the web app needs no server change.
* No row means status ``new``: the user has never seen the tour. Users that existed before the
  tour have no row either; the web app offers them at most a dismissible hint.
* No audit entries: these are display preferences, not security relevant changes.
"""

import re
from dataclasses import dataclass
from uuid import UUID

from mowdeck_contracts.v1 import PrincipalKind

from identity.application.context import RequestContext
from identity.application.deps import Deps
from identity.domain.errors import ForbiddenError, InvalidInput
from identity.domain.models import OnboardingProgress, OnboardingStatus

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
"""A step or hint key: lowercase letters, digits and dashes, at most 32 characters."""
MAX_KEYS = 32
"""At most this many skipped steps and this many dismissed hints."""


@dataclass(frozen=True, slots=True)
class OnboardingChoice:
    """What the web app stores: the whole state, replacing the previous one."""

    status: OnboardingStatus
    skipped_steps: list[str]
    dismissed_hints: list[str]


def checked_keys(values: list[str], what: str) -> list[str]:
    """``values`` without duplicates (first one wins); ``InvalidInput`` for a key of the wrong
    shape or for more than :data:`MAX_KEYS` keys."""
    keys = list(dict.fromkeys(values))
    if len(keys) > MAX_KEYS:
        msg = f"at most {MAX_KEYS} {what}"
        raise InvalidInput(msg)
    for key in keys:
        if not KEY_PATTERN.fullmatch(key):
            msg = f"{what}: {key[:40]!r} is no key (a-z, 0-9 and '-', at most 32 characters)"
            raise InvalidInput(msg)
    return keys


class OnboardingService:
    """Read and replace the caller's tour progress."""

    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    @staticmethod
    def _user(ctx: RequestContext) -> UUID:
        principal = ctx.principal
        if principal is None or principal.kind not in (PrincipalKind.USER, PrincipalKind.SUBUSER):
            msg = "only users have a setup tour"
            raise ForbiddenError(msg)
        return principal.subject

    async def get(self, ctx: RequestContext) -> OnboardingProgress | None:
        """The caller's progress; None while they never decided (status ``new``)."""
        user_id = self._user(ctx)
        async with self.deps.store.transaction() as tx:
            return await tx.onboarding.get(user_id=user_id)

    async def save(self, ctx: RequestContext, choice: OnboardingChoice) -> OnboardingProgress:
        """Replace the caller's progress. ``new`` cannot be stored (it means "no row")."""
        user_id = self._user(ctx)
        status = OnboardingStatus(choice.status)
        if status is OnboardingStatus.NEW:
            msg = "status 'new' cannot be stored; use active, dismissed or completed"
            raise InvalidInput(msg)
        skipped = checked_keys(choice.skipped_steps, "skipped steps")
        dismissed = checked_keys(choice.dismissed_hints, "dismissed hints")
        now = self.deps.clock()
        async with self.deps.store.transaction() as tx:
            found = await tx.onboarding.get(user_id=user_id)
            if found is None:
                progress = OnboardingProgress(user_id, status, skipped, dismissed, now, now)
                await tx.onboarding.add(progress)
                return progress
            found.status = status
            found.skipped_steps = skipped
            found.dismissed_hints = dismissed
            found.updated_at = now
            await tx.onboarding.update(found)
            return found

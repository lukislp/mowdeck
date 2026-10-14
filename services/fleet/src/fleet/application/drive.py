"""Manual drive sessions (ADR 0026): open and close the record, list the audit.

Realtime asks once per session (``fleet.drive.start``) and closes it at the end
(``fleet.drive.stop``); the Vision service does the same for an approved AI assist maneuver
(``origin = vision``, on behalf of the approving user, ADR 0033); the frames go straight to
the Device-Gateway and never pass through Fleet. Fleet decides who may drive (``can_drive``
on the mower for the subject, 404 semantics for invisible mowers) and keeps at most one open
session per mower.

A session that Realtime never closed (a crashed instance) stays open until ``expires_at``; the
next start for the mower closes it with ``expired``. Concurrent starts for the same mower are
serialised by a transaction lock on the mower, so two clients never both get a session.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from mowdeck_authz import mower
from mowdeck_contracts.v1 import (
    DriveEndReason,
    DriveError,
    DriveErrorCode,
    DriveSessionStarted,
    DriveSessionView,
    DriveStartReply,
    DriveStartRequest,
    DriveStopReply,
    DriveStopRequest,
    Principal,
    PrincipalKind,
    PrincipalRef,
)

from fleet.application.access import Access
from fleet.application.ports import Clock, Lt, Store
from fleet.domain.errors import ForbiddenError, NotFoundError
from fleet.domain.models import DriveSession
from fleet.domain.rules import CAN_DRIVE

_log = logging.getLogger("fleet.drive")

DRIVE_LOCK_NAMESPACE = 0x4472_6976_0000_0000  # "Driv": per-mower lock of the start check


def drive_lock_key(mower_id: UUID) -> int:
    """A signed 64 bit advisory lock key for the drive start of one mower."""
    value = (DRIVE_LOCK_NAMESPACE ^ (mower_id.int & 0xFFFF_FFFF_FFFF_FFFF)) & 0xFFFF_FFFF_FFFF_FFFF
    return value - (1 << 64) if value >= 1 << 63 else value


def _refused(code: DriveErrorCode, detail: str) -> DriveStartReply:
    return DriveStartReply(error=DriveError(code=code, detail=detail[:500]))


def drive_view(session: DriveSession) -> DriveSessionView:
    """The audit view of a session."""
    return DriveSessionView(
        session_id=session.id,
        mower_id=session.mower_id,
        started_by=PrincipalRef.model_validate(session.started_by),
        started_at=session.started_at,
        expires_at=session.expires_at,
        blade_on_allowed=session.blade_on_allowed,
        ended_at=session.ended_at,
        end_reason=DriveEndReason(session.end_reason) if session.end_reason else None,
        frames=session.frames,
        frames_dropped=session.frames_dropped,
        origin=session.origin,
    )


@dataclass(frozen=True, slots=True)
class DrivePage:
    """A page of drive sessions, newest first; ``next_before`` continues it."""

    items: list[DriveSessionView]
    next_before: datetime | None


class DriveService:
    """Use cases of manual drive sessions."""

    def __init__(self, store: Store, access: Access, clock: Clock) -> None:
        self.store = store
        self.access = access
        self.clock = clock

    async def start(self, request: DriveStartRequest) -> DriveStartReply:
        """``fleet.drive.start``: check ``can_drive`` for the subject and open a session."""
        ref = request.on_behalf_of
        if ref.kind == PrincipalKind.SERVICE:
            return _refused(DriveErrorCode.INVALID, "service principals hold no rights")
        principal = Principal(
            subject=ref.subject,
            account_id=request.account_id,
            kind=ref.kind,
            display_name=ref.display_name,
            token_id=ref.token_id,
        )
        try:
            await self.access.require(principal, CAN_DRIVE, mower(request.mower_id))
        except NotFoundError as exc:
            return _refused(DriveErrorCode.NOT_FOUND, exc.message)
        except ForbiddenError as exc:
            return _refused(DriveErrorCode.FORBIDDEN, exc.message)
        now = self.clock()
        async with self.store.transaction() as tx:
            target = await tx.mowers.get(id=request.mower_id, account_id=request.account_id)
            if target is None:
                return _refused(DriveErrorCode.NOT_FOUND, "mower not found")
            if not await tx.try_lock(drive_lock_key(target.id)):
                return _refused(DriveErrorCode.BUSY, "another drive session is starting")
            for open_session in await tx.drive_sessions.find(mower_id=target.id, ended_at=None):
                if open_session.expires_at > now:
                    return _refused(DriveErrorCode.BUSY, "the mower is driven in another session")
                await tx.drive_sessions.update_where(
                    {
                        "ended_at": open_session.expires_at,
                        "end_reason": DriveEndReason.EXPIRED.value,
                    },
                    id=open_session.id,
                )
            session = DriveSession(
                id=uuid4(),
                account_id=target.account_id,
                mower_id=target.id,
                started_by_subject=ref.subject,
                started_by=ref.model_dump(mode="json"),
                started_at=now,
                expires_at=now + timedelta(seconds=request.max_seconds),
                blade_on_allowed=request.blade_on_allowed,
                connection_id=request.connection_id,
                origin=request.origin,
            )
            await tx.drive_sessions.add(session)
        _log.info(
            "drive session started",
            extra={
                "session_id": str(session.id),
                "mower_id": str(session.mower_id),
                "origin": session.origin or "joystick",
            },
        )
        return DriveStartReply(
            started=DriveSessionStarted(
                session_id=session.id,
                mower_id=session.mower_id,
                account_id=session.account_id,
                started_at=session.started_at,
                expires_at=session.expires_at,
            )
        )

    async def stop(self, request: DriveStopRequest) -> DriveStopReply:
        """``fleet.drive.stop``: close an open session of the account (idempotent)."""
        async with self.store.transaction() as tx:
            changed = await tx.drive_sessions.update_where(
                {
                    "ended_at": request.ended_at,
                    "end_reason": request.reason.value,
                    "frames": request.frames,
                    "frames_dropped": request.frames_dropped,
                },
                id=request.session_id,
                account_id=request.account_id,
                mower_id=request.mower_id,
                ended_at=None,
            )
        return DriveStopReply(found=changed > 0)

    async def list_for_mower(
        self,
        account_id: UUID,
        mower_id: UUID,
        *,
        before: datetime | None = None,
        limit: int = 50,
    ) -> DrivePage:
        """Sessions of one mower, newest first (keyset on ``started_at``)."""
        where: dict[str, object] = {"account_id": account_id, "mower_id": mower_id}
        if before is not None:
            where["started_at"] = Lt(before)
        async with self.store.transaction() as tx:
            if await tx.mowers.get(id=mower_id, account_id=account_id) is None:
                raise NotFoundError("mower not found")
            rows = await tx.drive_sessions.find(
                order_by="started_at", descending=True, limit=limit + 1, **where
            )
        more = len(rows) > limit
        rows = rows[:limit]
        return DrivePage([drive_view(r) for r in rows], rows[-1].started_at if more else None)

    async def purge(self, retention: timedelta) -> int:
        """Delete sessions that started longer than ``retention`` ago (the command retention)."""
        cutoff = self.clock() - retention
        async with self.store.transaction() as tx:
            return await tx.drive_sessions.delete(started_at=Lt(cutoff))

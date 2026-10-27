"""Command line tools of notifications.

``python -m notifications.cli generate-vapid-keys`` prints a new VAPID key pair as environment
lines for the service (keep the private key secret, for example in a secret file referenced by
``MOWDECK_NOTIFICATIONS_VAPID_PRIVATE_KEY_FILE``)::

    MOWDECK_NOTIFICATIONS_VAPID_PRIVATE_KEY=...
    MOWDECK_NOTIFICATIONS_VAPID_PUBLIC_KEY=...

``python -m notifications.cli maintenance open --minutes 15`` opens the deploy maintenance window
(ADR 0055), ``maintenance close`` closes it and ``maintenance status`` prints it. They use the
database of the service (``MOWDECK_DATABASE_URL``), so the deploy runs them in the notifications
container (``docker compose exec -T notifications python -m notifications.cli maintenance open``).
The output never contains a secret.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import datetime

from mowdeck_contracts.v1 import utc_now
from mowdeck_service_kit.db import create_engine

from notifications.adapters.channels.vapid import generate_private_key, load_keys
from notifications.adapters.db.store import SqlStore
from notifications.application.maintenance import InvalidWindowError, close_window, open_window
from notifications.application.ports import Clock, Store
from notifications.domain.models import MaintenanceWindow
from notifications.settings import Settings


def generate_vapid_keys() -> tuple[str, str]:
    """A new pair: (private key, public key), both base64url."""
    private = generate_private_key()
    return private, load_keys(private).public_key


def describe(window: MaintenanceWindow | None, now: datetime) -> str:
    """One line for ``maintenance status`` and the other maintenance commands."""
    if window is None:
        return "maintenance: never opened"
    state = "open" if window.is_open(now) else "closed"
    return (
        f"maintenance: {state} (opened {window.opened_at.isoformat()}, "
        f"until {window.ends_at.isoformat()}, reason {window.reason})"
    )


async def run_maintenance(
    store: Store, action: str, *, minutes: int, reason: str, max_minutes: int, clock: Clock
) -> tuple[int, str]:
    """Run ``open``, ``close`` or ``status``; returns the exit code and the output line."""
    now = clock()
    if action == "open":
        try:
            window = await open_window(
                store, now, minutes=minutes, max_minutes=max_minutes, reason=reason
            )
        except InvalidWindowError as error:
            return 2, f"maintenance: {error}"
        return 0, describe(window, now)
    if action == "close":
        closed = await close_window(store, now)
        if closed is None:
            return 0, "maintenance: no window was open"
        return 0, describe(closed, now)
    return 0, describe(await store.maintenance_window(), now)


async def _maintenance_with_database(action: str, *, minutes: int, reason: str) -> tuple[int, str]:
    settings = Settings.from_env()
    if settings.database_url is None:
        return 2, "maintenance: MOWDECK_DATABASE_URL (or _FILE) is required"
    engine = create_engine(settings, pool_size=1, max_overflow=0)
    try:
        return await run_maintenance(
            SqlStore(engine),
            action,
            minutes=minutes,
            reason=reason,
            max_minutes=settings.notifications_maintenance_max_minutes,
            clock=utc_now,
        )
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command; returns the exit code."""
    parser = argparse.ArgumentParser(prog="python -m notifications.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("generate-vapid-keys", help="print a new VAPID key pair")
    maintenance = commands.add_parser(
        "maintenance", help="the deploy maintenance window (ADR 0055)"
    )
    actions = maintenance.add_subparsers(dest="action", required=True)
    opened = actions.add_parser("open", help="open the window (replaces an earlier one)")
    opened.add_argument("--minutes", type=int, default=15, help="length, expires by itself")
    opened.add_argument("--reason", default="deploy", help="free text, shown in status")
    actions.add_parser("close", help="close the window now")
    actions.add_parser("status", help="print the window")
    args = parser.parse_args(argv)
    if args.command == "generate-vapid-keys":
        private, public = generate_vapid_keys()
        sys.stdout.write(f"MOWDECK_NOTIFICATIONS_VAPID_PRIVATE_KEY={private}\n")
        sys.stdout.write(f"MOWDECK_NOTIFICATIONS_VAPID_PUBLIC_KEY={public}\n")
        return 0
    code, line = asyncio.run(
        _maintenance_with_database(
            args.action,
            minutes=getattr(args, "minutes", 0),
            reason=getattr(args, "reason", "deploy"),
        )
    )
    (sys.stdout if code == 0 else sys.stderr).write(line + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

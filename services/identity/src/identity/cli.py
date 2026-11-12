# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Operator commands: ``python -m identity.cli <command>`` (same settings as the service).

* ``create-user USERNAME --account-name NAME``: register an account with this owner, even
  when registration is closed.
* ``promote-owner USERNAME --account ID [--keep-current]``: make a member the owner; former
  owners become admins.
* ``rotate-keys [--now]``: announce a next signing key (promoted after an hour by the
  service), or with ``--now`` activate a new key at once.
* ``retire-key KID``: drop a signing key at once (emergency); tokens it signed fail.
* ``list-keys``: the signing keys and their status.
* ``reconcile [--account ID]``: make OpenFGA match the database.
* ``erasures [--all]``: the purges of deleted accounts that are not finished (ADR 0082): state,
  which services confirmed, attempts and the last error; ``--all`` adds the finished ones.
  ``erasures --scheduled``: the accounts scheduled for deletion whose purge has not begun, with
  the end of their grace period and whether it is over (works with the purge switched off).
* ``grant-platform-admin USERNAME [--revoke]``: make a user an operator admin
  (``platform:main#operator_admin``: manages accounts and plans, sees no mower data).

Passwords are read from ``MOWDECK_IDENTITY_CLI_PASSWORD``, from standard input when it is not a
terminal, or asked for interactively; they never appear in the process list.

In Compose: ``docker compose exec identity python -m identity.cli list-keys``.
"""

import argparse
import asyncio
import getpass
import os
import sys
from collections.abc import Sequence
from uuid import UUID

from mowdeck_service_kit.logging import configure_logging

from identity.application.accounts import Registration
from identity.application.context import SYSTEM
from identity.application.erasure import ScheduledDeletion, scheduled_deletions
from identity.domain.erasure import AccountErasure
from identity.domain.errors import IdentityError
from identity.runtime import Runtime
from identity.settings import Settings

PASSWORD_ENV = "MOWDECK_IDENTITY_CLI_PASSWORD"  # noqa: S105 - variable name, not a secret


def read_password() -> str:
    """The password from the environment, a pipe, or a prompt."""
    from_env = os.environ.get(PASSWORD_ENV)
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\r\n")
    return getpass.getpass("Password: ")


def erasure_line(erasure: AccountErasure) -> str:
    """One line per purge: account, state, services that confirmed of those required, retries."""
    line = (
        f"{erasure.account_id}  {erasure.state.value:<8}  started {erasure.started_at.isoformat()}"
        f"  confirmed {len(erasure.confirmed)}/{len(erasure.required)}"
    )
    if erasure.completed_at is not None:
        return f"{line}  erased {erasure.completed_at.isoformat()}"
    waiting = ", ".join(erasure.open_services)
    line += f"  waiting for {waiting}  attempts {erasure.attempts}"
    return f"{line}  last error: {erasure.last_error}" if erasure.last_error else line


def scheduled_line(scheduled: ScheduledDeletion, *, purging: bool) -> str:
    """One line per account scheduled for deletion: when its grace period ends and what happens
    then on this installation."""
    until = scheduled.delete_after.isoformat() if scheduled.delete_after else "unknown"
    line = f"{scheduled.account_id}  deleting  grace period until {until}"
    if not scheduled.due:
        return f"{line}  the owner can restore it"
    then = "purged at the next sweep" if purging else "stays as it is (the purge is off)"
    return f"{line}  over: {then}"


def parser() -> argparse.ArgumentParser:
    """The argument parser."""
    root = argparse.ArgumentParser(
        prog="python -m identity.cli", description=__doc__.split("\n")[0]
    )
    commands = root.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-user", help="register an account and its owner")
    create.add_argument("username")
    create.add_argument("--account-name", required=True)
    create.add_argument("--display-name")
    create.add_argument("--email")
    promote = commands.add_parser("promote-owner", help="make a member the owner of an account")
    promote.add_argument("username")
    promote.add_argument("--account", type=UUID, required=True)
    promote.add_argument("--keep-current", action="store_true", help="keep the current owners")
    rotate = commands.add_parser("rotate-keys", help="rotate the signing keys")
    rotate.add_argument("--now", action="store_true", help="activate the new key immediately")
    retire = commands.add_parser("retire-key", help="retire a signing key immediately")
    retire.add_argument("kid")
    commands.add_parser("list-keys", help="list the signing keys")
    reconcile = commands.add_parser("reconcile", help="make OpenFGA match the database")
    reconcile.add_argument("--account", type=UUID)
    erasures = commands.add_parser("erasures", help="the purges of deleted accounts")
    erasures.add_argument("--all", action="store_true", help="include the finished purges")
    erasures.add_argument(
        "--scheduled",
        action="store_true",
        help="the accounts scheduled for deletion whose purge has not begun",
    )
    platform = commands.add_parser(
        "grant-platform-admin", help="make a user an operator admin (platform:main)"
    )
    platform.add_argument("username")
    platform.add_argument("--revoke", action="store_true", help="take the operator admin away")
    return root


async def run_command(args: argparse.Namespace, runtime: Runtime) -> str:
    """Execute one command and return what to print."""
    services = await runtime.start()
    match args.command:
        case "create-user":
            registered = await services.accounts.register(
                Registration(
                    username=args.username,
                    password=read_password(),
                    account_name=args.account_name,
                    display_name=args.display_name,
                    email=args.email,
                ),
                SYSTEM,
                ignore_mode=True,
            )
            return f"created account {registered.account_id} with owner {registered.user_id}"
        case "promote-owner":
            await services.accounts.promote_owner(
                args.account, args.username, SYSTEM, keep_current=args.keep_current
            )
            return f"{args.username} is now an owner of {args.account}"
        case "rotate-keys":
            record = await runtime.keys.rotate_now(immediate=args.now)
            return f"{record.status.value} key {record.kid}"
        case "retire-key":
            await runtime.keys.retire(args.kid)
            return f"retired {args.kid}"
        case "list-keys":
            records = await runtime.keys.list_keys()
            return "\n".join(
                f"{r.kid}  {r.status.value:<8}  created {r.created_at.isoformat()}" for r in records
            )
        case "reconcile":
            if args.account is not None:
                result = await services.accounts.reconcile_account(args.account, SYSTEM)
            else:
                result = await services.accounts.reconcile_all()
            return f"reconciled: {result.writes} tuples written, {result.deletes} deleted"
        case "erasures" if args.scheduled:
            waiting = await scheduled_deletions(services.deps)
            if not waiting:
                return "no account is scheduled for deletion"
            purging = services.erasure is not None
            return "\n".join(scheduled_line(account, purging=purging) for account in waiting)
        case "erasures":
            if services.erasure is None:
                return "account erasure is not enabled on this installation"
            purges = await services.erasure.records(open_only=not args.all)
            if not purges:
                return "no purge is open" if not args.all else "no account was erased yet"
            return "\n".join(erasure_line(purge) for purge in purges)
        case "grant-platform-admin":
            user_id = await services.platform.set_admin(
                args.username, SYSTEM, grant=not args.revoke
            )
            verb = "is no longer" if args.revoke else "is now"
            return f"{args.username} ({user_id}) {verb} an operator admin of platform:main"
    msg = f"unknown command {args.command}"  # pragma: no cover - argparse rejects it first
    raise ValueError(msg)


async def amain(argv: Sequence[str] | None = None, runtime: Runtime | None = None) -> int:
    """Parse, run, print; returns the exit code."""
    args = parser().parse_args(argv)
    active = runtime or Runtime(Settings.from_env())
    # Logs to stderr: stdout carries only the command's result (ids, kids) for scripts.
    configure_logging(active.settings, stream=sys.stderr)
    try:
        print(await run_command(args, active))
    except IdentityError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    finally:
        await active.close()
    return 0


def main() -> None:
    """Console entry point."""
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()

"""Command line tools of notifications.

``python -m notifications.cli generate-vapid-keys`` prints a new VAPID key pair as environment
lines for the service (keep the private key secret, for example in a secret file referenced by
``MOWDECK_NOTIFICATIONS_VAPID_PRIVATE_KEY_FILE``)::

    MOWDECK_NOTIFICATIONS_VAPID_PRIVATE_KEY=...
    MOWDECK_NOTIFICATIONS_VAPID_PUBLIC_KEY=...
"""

import argparse
import sys
from collections.abc import Sequence

from notifications.adapters.channels.vapid import generate_private_key, load_keys


def generate_vapid_keys() -> tuple[str, str]:
    """A new pair: (private key, public key), both base64url."""
    private = generate_private_key()
    return private, load_keys(private).public_key


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command; returns the exit code."""
    parser = argparse.ArgumentParser(prog="python -m notifications.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("generate-vapid-keys", help="print a new VAPID key pair")
    args = parser.parse_args(argv)
    if args.command == "generate-vapid-keys":
        private, public = generate_vapid_keys()
        sys.stdout.write(f"MOWDECK_NOTIFICATIONS_VAPID_PRIVATE_KEY={private}\n")
        sys.stdout.write(f"MOWDECK_NOTIFICATIONS_VAPID_PUBLIC_KEY={public}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

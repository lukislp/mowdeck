"""Command line tools of Billing.

``python -m billing.cli send-fixture`` posts a signed, Stripe shaped
``customer.subscription.updated`` event to a webhook URL: the local check of the webhook path
without a Stripe account (provider ``fake``) or with stripe-mock. The secret is read from
``MOWDECK_BILLING_STRIPE_WEBHOOK_SECRET`` (or ``--secret``)::

    docker compose -f infra/compose/docker-compose.yml --profile billing exec billing \\
        python -m billing.cli send-fixture --account-id <uuid> --status past_due

Against real Stripe use the Stripe CLI instead (``stripe listen`` / ``stripe trigger``).
"""

import argparse
import os
import sys
import time
from collections.abc import Sequence
from uuid import UUID

import httpx

from billing.adapters.providers.fixtures import encode, sign, subscription_event

DEFAULT_URL = "http://127.0.0.1:8000/api/v1/billing/webhooks/stripe"
STATUSES = (
    "trialing",
    "active",
    "past_due",
    "unpaid",
    "canceled",
    "incomplete",
    "incomplete_expired",
    "paused",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m billing.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    send = commands.add_parser("send-fixture", help="post a signed subscription event")
    send.add_argument("--url", default=DEFAULT_URL)
    send.add_argument("--account-id", required=True, type=UUID)
    send.add_argument("--status", required=True, choices=STATUSES)
    send.add_argument("--plan", default="home")
    send.add_argument("--price-id", default="price_fixture_home")
    send.add_argument("--subscription-id", default=None)
    send.add_argument("--customer-id", default=None)
    send.add_argument(
        "--event-type",
        default="customer.subscription.updated",
        choices=[
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        ],
    )
    send.add_argument("--secret", default=None, help="default: the webhook secret setting")
    return parser


def send_fixture(args: argparse.Namespace, client: httpx.Client | None = None) -> int:
    """Build, sign and post the event; print the answer. 0 when the service accepted it."""
    secret = args.secret or os.environ.get("MOWDECK_BILLING_STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        print("no secret: set MOWDECK_BILLING_STRIPE_WEBHOOK_SECRET or --secret", file=sys.stderr)
        return 2
    now = int(time.time())
    account: UUID = args.account_id
    event = subscription_event(
        args.event_type,
        subscription_id=args.subscription_id or f"sub_fixture_{account.hex[:12]}",
        customer_id=args.customer_id or f"cus_fixture_{account.hex[:12]}",
        status=args.status,
        price_id=args.price_id,
        account_id=account,
        plan=args.plan,
        period_start=now - 86400,
        period_end=now + 29 * 86400,
        trial_end=now + 14 * 86400 if args.status == "trialing" else None,
    )
    body = encode(event)
    http = client or httpx.Client(timeout=10.0)
    try:
        response = http.post(
            args.url,
            content=body,
            headers={"Stripe-Signature": sign(body, secret), "Content-Type": "application/json"},
        )
    finally:
        if client is None:
            http.close()
    print(f"{event['type']} {args.status} -> {response.status_code} {response.text}")
    return 0 if response.is_success else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command."""
    args = _parser().parse_args(argv)
    return send_fixture(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

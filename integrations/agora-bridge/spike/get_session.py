# SPDX-License-Identifier: Apache-2.0
"""Spike helper: ask the Device-Gateway for one camera session and print it to stdout.

Sends the existing internal request ``gateway.mower.<id>.camera.session`` (core NATS
request/reply, ADR 0032) the way Fleet does, with a service principal. The reply holds a
short-lived Agora token: it goes to stdout only (to be piped into ``bridge_spike.py``), never to a
log. A redacted summary goes to stderr.

Usage (inside a throwaway container that reaches the stack's NATS)::

    python get_session.py --nats nats://127.0.0.1:4222 --mower <uuid> --account <uuid> \
        | python bridge_spike.py ...

Needs only ``nats-py``. Does not change mower state beyond what opening the camera in the web
app does (the stream token request makes the mower publish).
"""

import argparse
import asyncio
import json
import sys
import uuid

import nats

SPIKE_SUBJECT_ID = "00000000-0000-4000-8000-00000000a6e0"


async def fetch(nats_url: str, mower: str, account: str, timeout_s: float) -> dict:
    nc = await nats.connect(nats_url, connect_timeout=5)
    try:
        request = {
            "mower_id": mower,
            "account_id": account,
            "requested_by": {
                "subject": SPIKE_SUBJECT_ID,
                "kind": "service",
                "display_name": "camera-relay spike",
            },
        }
        msg = await nc.request(
            f"gateway.mower.{mower}.camera.session",
            json.dumps(request).encode(),
            timeout=timeout_s,
            headers={"Nats-Msg-Id": str(uuid.uuid4()), "Content-Type": "application/json"},
        )
        return json.loads(msg.data)
    finally:
        await nc.close()


def redacted(session: dict) -> dict:
    out = {k: v for k, v in session.items() if k not in {"token", "app_id", "encryption"}}
    out["token"] = "<redacted>" if session.get("token") else ""
    out["app_id"] = "<redacted>" if session.get("app_id") else ""
    out["encrypted"] = session.get("encryption") is not None
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nats", default="nats://127.0.0.1:4222")
    parser.add_argument("--mower", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    reply = asyncio.run(fetch(args.nats, args.mower, args.account, args.timeout))
    if reply.get("error"):
        print(f"session refused: {reply['error']}", file=sys.stderr)
        sys.exit(2)
    session = reply["session"]
    print(f"session: {json.dumps(redacted(session))}", file=sys.stderr)
    sys.stdout.write(json.dumps(session) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()

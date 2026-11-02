"""Measure what demo sandboxes cost (ADR 0076): CPU and memory of a running demo stack.

Usage (from the repository root, against a demo stack with sandboxes on, e.g. the e2e demo stack
``MOWDECK_E2E_DEMO=1 MOWDECK_E2E_PROJECT=mowdeck-sbx MOWDECK_E2E_PORT=8680 pnpm e2e:stack up``,
whose ``demo.env`` lifts the per-address and total limits)::

    uv run python tools/repo-checks/scripts/demo_sandbox_cost.py \
        --base http://localhost:8680 --project mowdeck-sbx --sandboxes 10 --seconds 60

Three phases, each sampled with ``docker stats`` every few seconds over ``--seconds``:

1. **baseline**: the stack as it is (the ready pool resting, no visitor);
2. **idle**: ``--sandboxes`` visitors clicked "Try the demo" and left (no socket open): their
   mowers rest in the Device-Gateway;
3. **active**: the same visitors with a live socket each, subscribed to their three mowers, and
   one of them mowing per sandbox (a ``start`` command), like a visitor watching the demo.

Prints the mean CPU (percent of one core) and memory of every container per phase and the
difference per sandbox. The numbers in ADR 0076 and docs/ops/demo.md came from this script.
"""

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from websockets.asyncio.client import ClientConnection, connect

REFRESH = {"X-Mowdeck-Refresh": "cookie-only", "X-Requested-With": "mowdeck-web"}


@dataclass
class Visitor:
    """One sandbox: its token, mowers and (in the active phase) its socket."""

    token: str
    account_id: str
    mowers: list[str]
    socket: ClientConnection | None = None
    received: int = 0
    reader: asyncio.Task[None] | None = field(default=None, repr=False)


def _units(value: str) -> float:
    """``12.5MiB`` / ``1.2GiB`` / ``800KiB`` as MiB."""
    number = float("".join(c for c in value if c.isdigit() or c == "."))
    if value.endswith("GiB"):
        return number * 1024
    if value.endswith("KiB"):
        return number / 1024
    if value.endswith("B") and not value.endswith("iB"):
        return number / 1024 / 1024
    return number


def sample(project: str) -> dict[str, tuple[float, float]]:
    """``{service: (cpu percent, memory MiB)}`` of the project's containers, once."""
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found: dict[str, tuple[float, float]] = {}
    for line in out.splitlines():
        row = json.loads(line)
        name: str = row["Name"]
        if not name.startswith(f"{project}-"):
            continue
        service = name.removeprefix(f"{project}-").rsplit("-", 1)[0]
        cpu = float(row["CPUPerc"].rstrip("%") or 0)
        mem = _units(row["MemUsage"].split("/")[0].strip())
        found[service] = (cpu, mem)
    return found


async def measure(project: str, seconds: float) -> dict[str, tuple[float, float]]:
    """Mean CPU and memory per service over ``seconds``."""
    rows: list[dict[str, tuple[float, float]]] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        rows.append(await asyncio.to_thread(sample, project))
        await asyncio.sleep(2)
    services = sorted({s for row in rows for s in row})
    return {
        s: (
            statistics.fmean(r[s][0] for r in rows if s in r),
            statistics.fmean(r[s][1] for r in rows if s in r),
        )
        for s in services
    }


async def enter(base: str) -> Visitor:
    """A new visitor clicks "Try the demo" (own cookie jar) and lists their mowers."""
    async with httpx.AsyncClient(base_url=base, timeout=60) as client:
        started = time.perf_counter()
        response = await client.post("/api/v1/identity/demo/session", headers=REFRESH)
        response.raise_for_status()
        took = time.perf_counter() - started
        token = response.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        me = (await client.get("/api/v1/identity/me", headers=auth)).json()
        mowers = (await client.get("/api/v1/fleet/mowers", headers=auth)).json()["items"]
    print(f"  sandbox {me['account_id']} in {took:.2f} s, {len(mowers)} mowers", flush=True)
    return Visitor(token, me["account_id"], [m["id"] for m in mowers])


async def watch(base: str, visitor: Visitor) -> None:
    """Open the visitor's socket, subscribe to their mowers and start one of them."""
    ws_url = base.replace("http", "ws", 1) + "/api/v1/realtime/ws"
    socket = await connect(ws_url, additional_headers={"Origin": base}, open_timeout=10)
    await socket.send(json.dumps({"type": "auth", "token": visitor.token}))
    await socket.send(
        json.dumps({"type": "subscribe", "mower_ids": visitor.mowers, "request_id": "s"})
    )
    visitor.socket = socket

    async def read() -> None:
        async for _ in socket:
            visitor.received += 1

    visitor.reader = asyncio.create_task(read())
    auth = {"Authorization": f"Bearer {visitor.token}"}
    mower = visitor.mowers[0]
    async with httpx.AsyncClient(base_url=base, timeout=30) as client:
        areas: list[str] = []
        for _ in range(30):  # the map comes with the mower's first connect
            found = await client.get(f"/api/v1/telemetry/mowers/{mower}/map", headers=auth)
            if found.status_code == 200:
                body = found.json()
                areas = [a["id"] for a in body.get("areas") or body.get("map", {}).get("areas", [])]
                if areas:
                    break
            await asyncio.sleep(1)
        started = await client.post(
            f"/api/v1/fleet/mowers/{mower}/commands",
            json={"command": {"kind": "start_mow", "area_ids": areas[:1]}},
            headers=auth,
        )
        if started.status_code != 202:
            print(f"  start refused: {started.status_code} {started.text[:200]}", flush=True)


def report(name: str, rows: dict[str, tuple[float, float]]) -> tuple[float, float]:
    cpu = sum(c for c, _ in rows.values())
    mem = sum(m for _, m in rows.values())
    print(f"\n{name}: CPU {cpu:.1f} % of one core, memory {mem:.0f} MiB")
    for service, (c, m) in sorted(rows.items(), key=lambda item: -item[1][0]):
        print(f"  {service:<16} {c:6.1f} %  {m:7.1f} MiB")
    return cpu, mem


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--base", default="http://localhost:8680")
    parser.add_argument("--project", default="mowdeck-sbx", type=str)
    parser.add_argument("--sandboxes", type=int, default=10)
    parser.add_argument("--seconds", type=float, default=60)
    args = parser.parse_args()
    if not args.project.replace("-", "").replace("_", "").isalnum():
        parser.error("invalid project name")

    print("baseline ...", flush=True)
    base_cpu, base_mem = report("baseline", await measure(args.project, args.seconds))
    print(f"\ncreating {args.sandboxes} sandboxes ...", flush=True)
    visitors = [await enter(args.base) for _ in range(args.sandboxes)]
    await asyncio.sleep(10)
    idle_cpu, idle_mem = report("idle", await measure(args.project, args.seconds))
    print(f"\nopening {args.sandboxes} sockets ...", flush=True)
    await asyncio.gather(*(watch(args.base, v) for v in visitors))
    await asyncio.sleep(10)
    active_cpu, active_mem = report("active", await measure(args.project, args.seconds))
    frames = sum(v.received for v in visitors)
    n = args.sandboxes
    print(
        f"\nper sandbox: idle {(idle_cpu - base_cpu) / n:.2f} % CPU, "
        f"{(idle_mem - base_mem) / n:.1f} MiB; active {(active_cpu - base_cpu) / n:.2f} % CPU, "
        f"{(active_mem - base_mem) / n:.1f} MiB; {frames} realtime frames received"
    )
    for visitor in visitors:
        if visitor.socket is not None:
            await visitor.socket.close()
        if visitor.reader is not None:
            visitor.reader.cancel()
    return 0


def _run() -> Any:
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(_run())

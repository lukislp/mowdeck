# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

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

Prints the mean CPU (percent of one core) and memory of every container per phase, the
difference per sandbox and service, and per phase the database's write rate (transactions and
rows written a second per database, WAL bytes a second, from ``pg_stat_database`` and
``pg_stat_wal`` through ``docker exec`` into the stack's ``postgres`` container) and the NATS
message rate (``/varz``, asked from a throwaway curl container on the stack's network).
``--hold`` keeps the watched sandboxes running that many seconds after the measurement, e.g. to
attach a profiler. The numbers in ADR 0076, docs/ops/demo.md and docs/ops/load-testing.md came
from this script.

``--lifecycle`` measures the claim and the release instead (ADR 0079): ``--sandboxes`` visitors
click "Try the demo" within ``--ramp`` seconds (each from its own address of 198.18.0.0/15 in
``X-Forwarded-For``, so it needs a stack that trusts the caller as its proxy, like the load test
stack of ``tests/load``), each opens its mower list and one mower right away, then all of them
log out at once. Prints the claim, first-view and logout times (median, p95, max), how long until
every sandbox is deleted and the pool is full again, and the CPU and database writes of each
window::

    uv run python tools/repo-checks/scripts/demo_sandbox_cost.py --lifecycle \
        --base http://localhost:8590 --project mowdeck-load --sandboxes 40 --ramp 60
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


_PG_COUNTERS = (
    "SELECT datname, xact_commit, tup_inserted, tup_updated, tup_deleted"
    " FROM pg_stat_database WHERE datname IS NOT NULL"
    " UNION ALL SELECT '~wal', wal_bytes::bigint, wal_records, 0, 0 FROM pg_stat_wal"
)


def pg_counters(project: str) -> dict[str, tuple[int, int, int, int]]:
    """Cumulative ``{database: (commits, inserted, updated, deleted)}`` and ``~wal``:
    ``(bytes, records, 0, 0)``; empty when the database cannot be asked."""
    done = subprocess.run(  # noqa: S603 - fixed arguments, the project name is checked
        [  # noqa: S607
            "docker",
            "exec",
            f"{project}-postgres-1",
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-AtF,",
            "-c",
            _PG_COUNTERS,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    found: dict[str, tuple[int, int, int, int]] = {}
    for line in done.stdout.splitlines():
        name, *numbers = line.split(",")
        if len(numbers) == 4:
            a, b, c, d = (int(n) for n in numbers)
            found[name] = (a, b, c, d)
    return found


CURL_IMAGE = "curlimages/curl:8.11.1"


def nats_messages(project: str) -> int | None:
    """NATS's cumulative ``in_msgs`` (``/varz`` on the stack's network), None when unknown."""
    done = subprocess.run(  # noqa: S603 - fixed arguments, the project name is checked
        [  # noqa: S607
            "docker",
            "run",
            "--rm",
            "--network",
            project,
            CURL_IMAGE,
            "-s",
            "http://nats:8222/varz",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return int(json.loads(done.stdout)["in_msgs"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


@dataclass
class Phase:
    """What one phase measured: CPU and memory per service, and the write rates."""

    services: dict[str, tuple[float, float]]
    seconds: float
    pg: dict[str, tuple[float, float, float, float]]
    """Per second: ``{database: (commits, inserted, updated, deleted)}``, ``~wal`` in bytes."""
    nats_per_second: float | None


async def measure(project: str, seconds: float) -> Phase:
    """Mean CPU and memory per service over ``seconds``, and the write rates meanwhile."""
    rows: list[dict[str, tuple[float, float]]] = []
    pg_before = await asyncio.to_thread(pg_counters, project)
    nats_before = await asyncio.to_thread(nats_messages, project)
    started = time.monotonic()
    deadline = started + seconds
    while time.monotonic() < deadline:
        rows.append(await asyncio.to_thread(sample, project))
        await asyncio.sleep(2)
    pg_after = await asyncio.to_thread(pg_counters, project)
    nats_after = await asyncio.to_thread(nats_messages, project)
    took = time.monotonic() - started
    services = sorted({s for row in rows for s in row})
    pg: dict[str, tuple[float, float, float, float]] = {}
    for name, (a, b, c, d) in pg_after.items():
        if name in pg_before:
            a0, b0, c0, d0 = pg_before[name]
            pg[name] = ((a - a0) / took, (b - b0) / took, (c - c0) / took, (d - d0) / took)
    nats = (
        (nats_after - nats_before) / took
        if nats_after is not None and nats_before is not None
        else None
    )
    return Phase(
        services={
            s: (
                statistics.fmean(r[s][0] for r in rows if s in r),
                statistics.fmean(r[s][1] for r in rows if s in r),
            )
            for s in services
        },
        seconds=took,
        pg=pg,
        nats_per_second=nats,
    )


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


def report(name: str, phase: Phase) -> tuple[float, float]:
    rows = phase.services
    cpu = sum(c for c, _ in rows.values())
    mem = sum(m for _, m in rows.values())
    print(f"\n{name}: CPU {cpu:.1f} % of one core, memory {mem:.0f} MiB")
    for service, (c, m) in sorted(rows.items(), key=lambda item: -item[1][0]):
        print(f"  {service:<16} {c:6.1f} %  {m:7.1f} MiB")
    if phase.pg:
        wal = phase.pg.get("~wal")
        print("  database writes per second (commits, rows inserted / updated / deleted):")
        for db, (commits, ins, upd, dele) in sorted(phase.pg.items()):
            if db != "~wal" and commits >= 0.05:
                print(f"    {db:<16} {commits:7.1f}  {ins:7.1f} / {upd:7.1f} / {dele:7.1f}")
        if wal is not None:
            print(f"    WAL              {wal[0] / 1024:7.1f} KiB/s, {wal[1]:7.1f} records/s")
    if phase.nats_per_second is not None:
        print(f"  NATS messages in  {phase.nats_per_second:7.1f} /s")
    return cpu, mem


def per_sandbox(base: Phase, active: Phase, n: int) -> None:
    """The difference per sandbox and service between the baseline and a phase."""
    print("\nper watched sandbox and service (percent of one core):")
    services = sorted(
        active.services,
        key=lambda s: -(active.services[s][0] - base.services.get(s, (0.0, 0.0))[0]),
    )
    for service in services:
        delta = (active.services[service][0] - base.services.get(service, (0.0, 0.0))[0]) / n
        if abs(delta) >= 0.01:
            print(f"  {service:<16} {delta:6.2f} %")


def percentiles(values: list[float]) -> str:
    """``median / p95 / max`` of ``values`` in milliseconds."""
    if not values:
        return "-"
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return (
        f"median {statistics.median(ordered) * 1000:.0f} ms, p95 {p95 * 1000:.0f} ms, "
        f"max {ordered[-1] * 1000:.0f} ms"
    )


def sandbox_states(project: str) -> dict[str, int]:
    """``{state: count}`` of Identity's ``demo_sandboxes`` (empty when it cannot be asked)."""
    done = subprocess.run(  # noqa: S603 - fixed arguments, the project name is checked
        [  # noqa: S607
            "docker",
            "exec",
            f"{project}-postgres-1",
            "psql",
            "-U",
            "postgres",
            "-d",
            "identity",
            "-AtF,",
            "-c",
            "SELECT state, count(*) FROM demo_sandboxes GROUP BY 1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    found: dict[str, int] = {}
    for line in done.stdout.splitlines():
        state, _, count = line.partition(",")
        if count.isdigit():
            found[state] = int(count)
    return found


@dataclass
class Claimed:
    """One visitor of the lifecycle run: its refresh token and how long each step took."""

    refresh: str | None = None
    claim: float | None = None
    first_view: float | None = None
    logout: float | None = None
    errors: list[str] = field(default_factory=list)


async def warm(client: httpx.AsyncClient) -> None:
    """Open the client's connection with a static file, so the measured request does not
    include connecting (Docker Desktop's port forwarding takes a few hundred ms per
    connection, which would swamp what the services do)."""
    await client.get("/config.json")


async def claim_and_look(base: str, address: str, delay: float, visitor: Claimed) -> None:
    """After ``delay``: "Try the demo", then the mower list and one mower, as the web app."""
    await asyncio.sleep(delay)
    headers = {"X-Forwarded-For": address, "X-Requested-With": "mowdeck-web"}
    async with httpx.AsyncClient(base_url=base, timeout=60, headers=headers) as client:
        await warm(client)
        started = time.perf_counter()
        response = await client.post("/api/v1/identity/demo/session", json={})
        if response.status_code != 200:
            visitor.errors.append(f"claim {response.status_code}")
            return
        visitor.claim = time.perf_counter() - started
        body = response.json()
        visitor.refresh = body.get("refresh_token")
        auth = {"Authorization": f"Bearer {body['access_token']}"}
        started = time.perf_counter()
        listed = await client.get("/api/v1/fleet/mowers", headers=auth)
        if listed.status_code != 200:
            visitor.errors.append(f"mowers {listed.status_code}")
            return
        items = listed.json()["items"]
        if items:
            mower = items[0]["id"]
            answers = await asyncio.gather(
                client.get(f"/api/v1/telemetry/mowers/{mower}/state", headers=auth),
                client.get(f"/api/v1/fleet/mowers/{mower}", headers=auth),
                client.get("/api/v1/identity/me", headers=auth),
            )
            visitor.errors.extend(
                f"view {a.status_code}" for a in answers if a.status_code not in (200, 404)
            )
        visitor.first_view = time.perf_counter() - started


async def log_out(base: str, address: str, visitor: Claimed, start: asyncio.Event) -> None:
    """``POST /identity/session/logout`` with the visitor's refresh token, once ``start`` is set
    (every visitor's connection is open by then)."""
    if visitor.refresh is None:
        return
    headers = {"X-Forwarded-For": address, "X-Requested-With": "mowdeck-web"}
    async with httpx.AsyncClient(base_url=base, timeout=60, headers=headers) as client:
        await warm(client)
        await start.wait()
        started = time.perf_counter()
        response = await client.post(
            "/api/v1/identity/session/logout", json={"refresh_token": visitor.refresh}
        )
        if response.status_code != 204:
            visitor.errors.append(f"logout {response.status_code}")
            return
        visitor.logout = time.perf_counter() - started


async def sampled(project: str, work: Any) -> tuple[Any, Phase]:
    """Run ``work`` (an awaitable) while sampling like :func:`measure`; its result and phase."""
    rows: list[dict[str, tuple[float, float]]] = []
    pg_before = await asyncio.to_thread(pg_counters, project)
    started = time.monotonic()
    task = asyncio.ensure_future(work)
    while not task.done():
        rows.append(await asyncio.to_thread(sample, project))
        await asyncio.wait({task}, timeout=2)
    pg_after = await asyncio.to_thread(pg_counters, project)
    took = time.monotonic() - started
    services = sorted({s for row in rows for s in row})
    pg = {
        name: tuple((x - y) / took for x, y in zip(after, pg_before[name], strict=True))
        for name, after in pg_after.items()
        if name in pg_before
    }
    phase = Phase(
        services={
            s: (
                statistics.fmean(r[s][0] for r in rows if s in r),
                statistics.fmean(r[s][1] for r in rows if s in r),
            )
            for s in services
        },
        seconds=took,
        pg=pg,  # type: ignore[arg-type]
        nats_per_second=None,
    )
    return task.result(), phase


async def wait_for(project: str, done: Any, limit: float) -> float | None:
    """Seconds until ``done(states)`` holds (polled every 2 s), None after ``limit``."""
    started = time.monotonic()
    while time.monotonic() - started < limit:
        if done(await asyncio.to_thread(sandbox_states, project)):
            return time.monotonic() - started
        await asyncio.sleep(2)
    return None


async def lifecycle(args: argparse.Namespace) -> int:
    """The claim and release measurement (see the module docstring)."""
    project, n = args.project, args.sandboxes
    print(f"sandboxes before: {sandbox_states(project)}", flush=True)
    print(f"waiting for the pool of {args.pool} ...", flush=True)
    await wait_for(project, lambda s: s.get("ready", 0) >= args.pool, 600)
    visitors = [Claimed() for _ in range(n)]
    offset = args.address_offset
    addresses = [f"198.18.{(offset + i) // 250 % 256}.{1 + (offset + i) % 250}" for i in range(n)]
    print(f"\n{n} visitors claim within {args.ramp:.0f} s ...", flush=True)
    _, claim_phase = await sampled(
        project,
        asyncio.gather(
            *(
                claim_and_look(args.base, addresses[i], args.ramp * i / max(1, n - 1), v)
                for i, v in enumerate(visitors)
            )
        ),
    )
    claims = [v.claim for v in visitors if v.claim is not None]
    views = [v.first_view for v in visitors if v.first_view is not None]
    report("claim window", claim_phase)
    print(f"\nclaim: {len(claims)} of {n}, {percentiles(claims)}")
    print(f"first view (mower list, mower, state, me): {percentiles(views)}")
    print(f"sandboxes after the claims: {sandbox_states(project)}", flush=True)
    refilled = await wait_for(project, lambda s: s.get("ready", 0) >= args.pool, 600)
    print(f"pool full again {refilled if refilled is None else round(refilled)} s later")
    await asyncio.sleep(args.hold)
    print(f"\n{len(claims)} visitors log out at once ...", flush=True)
    start = asyncio.Event()

    async def release() -> None:
        tasks = [
            asyncio.create_task(log_out(args.base, addresses[i], v, start))
            for i, v in enumerate(visitors)
        ]
        await asyncio.sleep(5)  # every connection open
        start.set()
        await asyncio.gather(*tasks)

    await release()
    logouts = [v.logout for v in visitors if v.logout is not None]
    print(f"logout: {len(logouts)}, {percentiles(logouts)}", flush=True)

    async def deleted() -> float | None:
        return await wait_for(
            project, lambda s: not s.get("active", 0) and not s.get("purging", 0), 900
        )

    gone, release_phase = await sampled(project, deleted())
    report("release window (logout until every sandbox is deleted)", release_phase)
    print(f"\nevery sandbox deleted {gone if gone is None else round(gone)} s after the logouts")
    errors = [e for v in visitors for e in v.errors]
    print(f"errors: {len(errors)} {sorted(set(errors))}")
    print(f"sandboxes at the end: {sandbox_states(project)}", flush=True)
    return 1 if errors else 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--base", default="http://localhost:8680")
    parser.add_argument("--project", default="mowdeck-sbx", type=str)
    parser.add_argument("--sandboxes", type=int, default=10)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--hold", type=float, default=0, help="seconds to keep watching after")
    parser.add_argument("--lifecycle", action="store_true", help="measure claim and release")
    parser.add_argument("--ramp", type=float, default=60, help="lifecycle: arrival window")
    parser.add_argument("--pool", type=int, default=8, help="lifecycle: the stack's pool size")
    parser.add_argument(
        "--address-offset", type=int, default=0, help="lifecycle: first visitor address"
    )
    args = parser.parse_args()
    if not args.project.replace("-", "").replace("_", "").isalnum():
        parser.error("invalid project name")
    if args.lifecycle:
        return await lifecycle(args)

    print("baseline ...", flush=True)
    baseline = await measure(args.project, args.seconds)
    base_cpu, base_mem = report("baseline", baseline)
    print(f"\ncreating {args.sandboxes} sandboxes ...", flush=True)
    visitors = [await enter(args.base) for _ in range(args.sandboxes)]
    await asyncio.sleep(10)
    idle_cpu, idle_mem = report("idle", await measure(args.project, args.seconds))
    print(f"\nopening {args.sandboxes} sockets ...", flush=True)
    # The first socket alone: a fresh Realtime fetches Identity's JWKS on the first sign-in, and
    # sign-ins racing that first fetch are refused (4401), which would leave mowers resting.
    await watch(args.base, visitors[0])
    for _ in range(100):  # until its "ready" (Realtime has the keys) or 10 s
        if visitors[0].received:
            break
        await asyncio.sleep(0.1)
    await asyncio.gather(*(watch(args.base, v) for v in visitors[1:]))
    await asyncio.sleep(10)
    frames_before = sum(v.received for v in visitors)
    active = await measure(args.project, args.seconds)
    active_cpu, active_mem = report("active", active)
    frames = sum(v.received for v in visitors) - frames_before
    n = args.sandboxes
    per_sandbox(baseline, active, n)
    print(
        f"\nper sandbox: idle {(idle_cpu - base_cpu) / n:.2f} % CPU, "
        f"{(idle_mem - base_mem) / n:.1f} MiB; active {(active_cpu - base_cpu) / n:.2f} % CPU, "
        f"{(active_mem - base_mem) / n:.1f} MiB; "
        f"{frames / active.seconds:.1f} realtime frames a second received while active, "
        f"{sum(1 for v in visitors if v.received > 3)} of {n} live views received states",
        flush=True,
    )
    if args.hold > 0:
        print(f"\nholding the watched sandboxes for {args.hold:.0f} s ...", flush=True)
        await asyncio.sleep(args.hold)
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

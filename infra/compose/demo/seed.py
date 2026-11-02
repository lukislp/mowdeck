"""Seed job of the public demo (ADR 0068): the demo account, its simulated mowers and a week
of history. Standard library only; runs once per (re)start of a demo stack, before the
Device-Gateway starts (``docker-compose.demo.yml``, service ``demo-seed``).

1. Identity creates the demo account and its user (``POST /identity/demo/seed``).
2. "Try the demo" gives the job a session of that user, like every visitor; the setup tour is
   marked done.
3. Fleet registers the simulated mowers (each in its own garden); the seed secret lets these
   calls pass the demo policy, which refuses them to visitors.
4. The Scheduler gets a weekly schedule per mower, and the user's notification preferences are
   stored (the defaults): the demo has no delivery target, and Notifications fills the in-app
   inbox only for users who saved preferences or registered a target.
5. Telemetry simulates and ingests the past days (tracks, coverage, heatmaps, health).

Every step is idempotent: an account that already has mowers is left as it is, so a restarted
job does not double anything. The job talks to the services inside the Compose network only;
the seed secret never passes the gateway (Traefik strips the header from outside requests).

With ``DEMO_SANDBOXES`` true (the demo stack's default, ADR 0076) every visitor gets an own
sandbox account that Identity fills itself (``identity.adapters.demo_sandbox``, the same garden
as :data:`MOWERS`); the job then only waits until the services are ready and prepares nothing.

Environment: ``MOWDECK_DEMO_SEED_TOKEN`` (or ``MOWDECK_DEMO_SEED_TOKEN_FILE``), optionally
``DEMO_HISTORY_DAYS`` (default 7), ``DEMO_SANDBOXES`` (default false here; the compose file sets
it) and ``DEMO_<SERVICE>_URL`` for other service addresses.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SEED_HEADER = "X-MowDeck-Demo-Seed"
TIMEOUT_S = 20.0
HISTORY_TIMEOUT_S = 600.0
READY_WAIT_S = 180.0

ORIGIN = (48.137154, 11.576124)
"""Where the demo neighbourhood lies (the simulator's default origin)."""

MOWERS: list[dict[str, Any]] = [
    {
        "name": "Clover",
        "connection_options": {"garden": "family", "bridge": True},
        "offset_m": (0.0, 0.0),
        "schedule": {"days_of_week": ["mon", "wed", "fri"], "start_time": "09:00:00"},
    },
    {
        "name": "Juniper",
        "connection_options": {"garden": "orchard", "bridge": True, "battery_percent": 64.0},
        "offset_m": (70.0, 25.0),
        "schedule": {"days_of_week": ["tue", "sat"], "start_time": "08:30:00"},
    },
    {
        "name": "Pebble",
        "connection_options": {
            "garden": "default",
            "firmware_available": "sim-1.1.0",
            "firmware_release_notes": "Smoother turns at the lane ends and a quieter dock.",
        },
        "offset_m": (-55.0, 40.0),
        "schedule": {"days_of_week": ["thu", "sun"], "start_time": "10:00:00"},
    },
]
"""The demo mowers: name, simulator options, where the garden lies relative to ``ORIGIN``."""


def _url(service: str) -> str:
    default = f"http://{service}:8000/api/v1/{service}"
    return os.environ.get(f"DEMO_{service.upper().replace('-', '_')}_URL", default)


def _seed_token() -> str:
    path = os.environ.get("MOWDECK_DEMO_SEED_TOKEN_FILE")
    token = Path(path).read_text(encoding="utf-8").strip() if path else ""
    token = token or os.environ.get("MOWDECK_DEMO_SEED_TOKEN", "").strip()
    if not token:
        sys.exit("demo-seed: MOWDECK_DEMO_SEED_TOKEN is not set")
    return token


def _log(message: str) -> None:
    print(f"demo-seed: {message}", flush=True)


class Api:
    """JSON over HTTP with the seed secret and, once signed in, the demo user's token."""

    def __init__(self, seed_token: str) -> None:
        self.seed_token = seed_token
        self.access_token: str | None = None

    def call(
        self,
        method: str,
        url: str,
        body: Any = None,
        *,
        seed: bool = False,
        timeout: float = TIMEOUT_S,
    ) -> Any:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if seed:
            headers[SEED_HEADER] = self.seed_token
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310 - fixed internal URLs
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            msg = f"{method} {url}: {exc.code} {detail}"
            raise RuntimeError(msg) from exc
        return json.loads(raw) if raw else None


def _wait_ready(services: list[str]) -> None:
    deadline = time.monotonic() + READY_WAIT_S
    for service in services:
        url = _url(service).split("/api/")[0] + "/readyz"
        while True:
            try:
                with urllib.request.urlopen(url, timeout=3):  # noqa: S310 - internal URL
                    break
            except (urllib.error.URLError, OSError):
                if time.monotonic() > deadline:
                    sys.exit(f"demo-seed: {service} is not ready")
                time.sleep(2)


def _origin(offset_m: tuple[float, float]) -> tuple[float, float]:
    east, north = offset_m
    lat = ORIGIN[0] + north / 111_320.0
    lon = ORIGIN[1] + east / (111_320.0 * 0.6678)  # cos(48.14 deg)
    return round(lat, 7), round(lon, 7)


PREFERENCE_FIELDS = (
    "enabled_kinds",
    "all_mowers",
    "mower_ids",
    "quiet_hours",
    "timezone",
    "language",
    "channels",
)
"""The fields of ``PUT /notifications/preferences`` (``GET`` answers them plus read-only ones)."""


def _store_notification_preferences(api: Api) -> None:
    """Store the demo user's notification defaults once, so the inbox fills (see step 4)."""
    url = f"{_url('notifications')}/preferences"
    current = api.call("GET", url)
    if current.get("stored"):
        return
    api.call("PUT", url, {key: current[key] for key in PREFERENCE_FIELDS})
    _log("notification preferences stored (the defaults: the inbox fills)")


def _sandboxes() -> bool:
    """Demo sandboxes (ADR 0076): Identity fills every visitor's own account itself."""
    return os.environ.get("DEMO_SANDBOXES", "false").strip().lower() in ("1", "true", "yes", "on")


def main() -> int:
    api = Api(_seed_token())
    _wait_ready(["identity", "fleet", "scheduler", "telemetry", "notifications"])
    if _sandboxes():
        _log("demo sandboxes: every visitor gets an own account; no shared demo account")
        return 0
    seeded = api.call("POST", f"{_url('identity')}/demo/seed", seed=True)
    _log(f"account {seeded['account_id']} ({'created' if seeded['created'] else 'existing'})")
    session = api.call("POST", f"{_url('identity')}/demo/session")
    api.access_token = session["access_token"]
    # Visitors land on the mowers, not in the setup tour of a new account (ADR 0062).
    api.call("PUT", f"{_url('identity')}/me/onboarding", {"status": "completed"})

    existing = api.call("GET", f"{_url('fleet')}/mowers")["items"]
    if existing:
        _log(f"the account has {len(existing)} mowers already; no new mowers")
        _store_notification_preferences(api)
        return 0

    created: list[dict[str, Any]] = []
    for spec in MOWERS:
        lat, lon = _origin(spec["offset_m"])
        options = {**spec["connection_options"], "origin_lat": lat, "origin_lon": lon}
        mower = api.call(
            "POST",
            f"{_url('fleet')}/mowers",
            {"name": spec["name"], "connection_kind": "simulated", "connection_options": options},
            seed=True,
        )
        created.append({**spec, "id": mower["id"], "options": options})
        _log(f"mower {spec['name']} ({mower['id']}) in the {options['garden']} garden")

    for mower in created:
        schedule = {
            "name": f"{mower['name']}'s routine",
            "target_type": "mower",
            "target_id": mower["id"],
            "kind": "weekly",
            "duration_minutes": 180,
            "timezone": "Europe/Berlin",
            "rotation_step_deg": 45,
            **mower["schedule"],
        }
        api.call("POST", f"{_url('scheduler')}/schedules", schedule)
    _log(f"{len(created)} schedules")
    # After the mowers: Notifications admits a user who may view a mower.
    _store_notification_preferences(api)

    days = int(os.environ.get("DEMO_HISTORY_DAYS", "7"))
    history = api.call(
        "POST",
        f"{_url('telemetry')}/demo/history",
        {
            "mowers": [
                {
                    "mower_id": m["id"],
                    "account_id": seeded["account_id"],
                    "connection_options": m["options"],
                }
                for m in created
            ],
            "days": days,
        },
        seed=True,
        timeout=HISTORY_TIMEOUT_S,
    )
    _log(f"{days} days of history: {history['stored']} of {history['states']} states stored")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        sys.exit(f"demo-seed: {exc}")

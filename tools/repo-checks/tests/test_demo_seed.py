"""The seed job of the public demo (infra/compose/demo/seed.py, ADR 0068) against a recorded API."""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ACCOUNT = "11111111-1111-4111-8111-111111111111"
PREFERENCES = {
    "enabled_kinds": ["battery_low", "mowing_finished", "tracking_started"],
    "all_mowers": True,
    "mower_ids": [],
    "quiet_hours": None,
    "timezone": "UTC",
    "language": "en",
    "channels": ["apns", "ntfy", "webpush"],
}


def _load(repo_root: Path) -> ModuleType:
    path = repo_root / "infra" / "compose" / "demo" / "seed.py"
    spec = importlib.util.spec_from_file_location("demo_seed", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Recorder:
    """Answers like the services and records every call (method, path, body, seed header)."""

    def __init__(self, existing: int = 0, *, stored: bool = False) -> None:
        self.calls: list[tuple[str, str, Any, bool]] = []
        self.existing = existing
        self.created = 0
        self.stored = stored

    def __call__(
        self, method: str, url: str, body: Any = None, *, seed: bool = False, **_: Any
    ) -> Any:
        path = url.split("/api/v1", 1)[1]
        self.calls.append((method, path, body, seed))
        if path == "/identity/demo/seed":
            return {"account_id": ACCOUNT, "user_id": "u", "created": True}
        if path == "/identity/demo/session":
            return {"access_token": "token"}
        if path == "/fleet/mowers" and method == "GET":
            return {"items": [{"id": f"m{i}"} for i in range(self.existing)]}
        if path == "/fleet/mowers":
            self.created += 1
            return {"id": f"mower-{self.created}"}
        if path == "/telemetry/demo/history":
            return {"states": 10, "stored": 10}
        if path == "/notifications/preferences" and method == "GET":
            return {**PREFERENCES, "stored": self.stored, "available_kinds": ["battery_low"]}
        return {}


@pytest.fixture
def seed(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = _load(repo_root)
    monkeypatch.setenv("MOWDECK_DEMO_SEED_TOKEN", "secret")
    monkeypatch.setattr(module, "_wait_ready", lambda services: None)
    return module


def test_a_fresh_demo_gets_mowers_schedules_and_history(
    seed: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    monkeypatch.setattr(seed.Api, "call", recorder)
    assert seed.main() == 0
    paths = [(method, path) for method, path, _, _ in recorder.calls]
    assert paths[:4] == [
        ("POST", "/identity/demo/seed"),
        ("POST", "/identity/demo/session"),
        ("PUT", "/identity/me/onboarding"),
        ("GET", "/fleet/mowers"),
    ]
    mowers = [c for c in recorder.calls if c[:2] == ("POST", "/fleet/mowers")]
    assert len(mowers) == len(seed.MOWERS) == 3
    # Registering mowers is refused to visitors: only the seed secret lets it pass.
    assert all(c[3] for c in mowers)
    gardens = {c[2]["connection_options"]["garden"] for c in mowers}
    assert gardens == {"family", "orchard", "default"}
    assert all(c[2]["connection_kind"] == "simulated" for c in mowers)
    schedules = [c for c in recorder.calls if c[1] == "/scheduler/schedules"]
    assert len(schedules) == 3 and not any(c[3] for c in schedules)
    history = [c for c in recorder.calls if c[1] == "/telemetry/demo/history"]
    assert len(history) == 1 and history[0][3]
    assert history[0][2]["days"] == 7
    assert {m["account_id"] for m in history[0][2]["mowers"]} == {ACCOUNT}
    # The demo has no delivery target: stored defaults make the user a recipient of the inbox.
    puts = [c for c in recorder.calls if c[:2] == ("PUT", "/notifications/preferences")]
    assert len(puts) == 1 and puts[0][2] == PREFERENCES and not puts[0][3]
    assert paths.index(("PUT", "/notifications/preferences")) > paths.index(
        ("POST", "/scheduler/schedules")
    )


def test_a_seeded_account_is_left_alone(seed: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(existing=3)
    monkeypatch.setattr(seed.Api, "call", recorder)
    assert seed.main() == 0
    posts = [path for method, path, _, _ in recorder.calls if method == "POST"]
    assert posts == ["/identity/demo/seed", "/identity/demo/session"]
    assert recorder.created == 0
    puts = [path for method, path, _, _ in recorder.calls if method == "PUT"]
    assert puts == ["/identity/me/onboarding", "/notifications/preferences"]


def test_saved_preferences_are_kept(seed: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(existing=3, stored=True)
    monkeypatch.setattr(seed.Api, "call", recorder)
    assert seed.main() == 0
    assert ("PUT", "/notifications/preferences") not in [c[:2] for c in recorder.calls]


def test_the_seed_needs_its_secret(seed: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOWDECK_DEMO_SEED_TOKEN")
    monkeypatch.delenv("MOWDECK_DEMO_SEED_TOKEN_FILE", raising=False)
    with pytest.raises(SystemExit, match="MOWDECK_DEMO_SEED_TOKEN"):
        seed.main()


def test_the_gardens_lie_apart(seed: ModuleType) -> None:
    origins = {seed._origin(m["offset_m"]) for m in seed.MOWERS}
    assert len(origins) == len(seed.MOWERS)

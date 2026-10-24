"""The LiteLLM loader heals a half-initialised module and serialises imports."""

import importlib
import os
import sys
import threading
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from vision.adapters.providers import llm


def _complete_module() -> Any:
    return SimpleNamespace(
        acompletion=object(), exceptions=object(), supports_response_schema=lambda **_: True
    )


@pytest.fixture(autouse=True)
def _clean_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [n for n in sys.modules if n == "litellm" or n.startswith("litellm.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(llm, "_configured", False)


def _module(**attrs: object) -> ModuleType:
    """A module object with the given attributes (what ``sys.modules`` holds)."""
    module = ModuleType("litellm")
    for name, value in attrs.items():
        setattr(module, name, value)
    return module


def test_loader_replaces_a_partially_initialised_module(monkeypatch: pytest.MonkeyPatch) -> None:
    partial = SimpleNamespace(acompletion=object())  # no ``exceptions``: the deadlock leftover
    monkeypatch.setitem(sys.modules, "litellm", partial)
    monkeypatch.setitem(sys.modules, "litellm.utils", SimpleNamespace())
    fresh = _complete_module()
    imported: list[str] = []

    def fake_import(name: str) -> Any:
        imported.append(name)
        sys.modules["litellm"] = fresh
        return fresh

    monkeypatch.setattr(importlib, "import_module", fake_import)
    assert llm.load_litellm() is fresh
    assert imported == ["litellm"]
    assert "litellm.utils" not in sys.modules
    assert fresh.suppress_debug_info is True


def test_loader_keeps_the_cost_map_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP", raising=False)
    seen: list[str | None] = []

    def fake_import(name: str) -> Any:
        seen.append(os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP"))
        sys.modules["litellm"] = module = _complete_module()
        return module

    monkeypatch.setattr(importlib, "import_module", fake_import)
    llm.load_litellm()
    assert seen == ["True"]


def test_loader_respects_an_explicit_cost_map_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "False")

    def fake_import(name: str) -> Any:
        sys.modules["litellm"] = module = _complete_module()
        return module

    monkeypatch.setattr(importlib, "import_module", fake_import)
    llm.load_litellm()
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "False"


def test_loader_drops_the_module_when_the_import_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_import(name: str) -> Any:
        sys.modules["litellm"] = _module()
        msg = "boom"
        raise ImportError(msg)

    monkeypatch.setattr(importlib, "import_module", failing_import)
    with pytest.raises(ImportError):
        llm.load_litellm()
    assert "litellm" not in sys.modules


def test_loader_rejects_an_import_that_stays_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    def partial_import(name: str) -> Any:
        module = _module(acompletion=object())
        sys.modules["litellm"] = module
        return module

    monkeypatch.setattr(importlib, "import_module", partial_import)
    with pytest.raises(ImportError, match="partial"):
        llm.load_litellm()
    assert "litellm" not in sys.modules


def test_concurrent_loaders_import_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = _complete_module()
    calls = 0
    started = threading.Event()

    def slow_import(name: str) -> Any:
        nonlocal calls
        calls += 1
        started.set()
        sys.modules["litellm"] = fresh
        return fresh

    monkeypatch.setattr(importlib, "import_module", slow_import)
    results: list[Any] = []
    threads = [
        threading.Thread(target=lambda: results.append(llm.load_litellm())) for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == 1
    assert all(r is fresh for r in results)


async def test_startup_imports_litellm_on_the_main_thread_before_subscribing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vision_support import harness

    from vision import main

    seen: list[tuple[bool, int]] = []

    def fake_load() -> Any:
        seen.append((threading.current_thread() is threading.main_thread(), len(subscribed)))
        return _complete_module()

    subscribed: list[object] = []
    real_subscribe_all = main.subscribe_all  # type: ignore[attr-defined]

    async def recording_subscribe_all(*args: Any, **kwargs: Any) -> Any:
        result = await real_subscribe_all(*args, **kwargs)
        subscribed.append(result)
        return result

    monkeypatch.setattr(main, "load_litellm", fake_load)
    monkeypatch.setattr(main, "subscribe_all", recording_subscribe_all)
    async with harness(warm_litellm=True):
        pass
    # Once, on the main thread, before any subscription (nothing else imports concurrently).
    assert seen == [(True, 0)]
    assert subscribed, "the startup went on after the import"


async def test_startup_survives_a_failed_import_and_logs_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from vision_support import harness

    from vision import main

    def failing_load() -> Any:
        raise ImportError("deadlock detected by _ModuleLock('litellm.utils')")

    monkeypatch.setattr(main, "load_litellm", failing_load)
    async with harness(warm_litellm=True) as h:
        response = await h.client.get("/readyz")
        assert response.status_code == 200
    out = capsys.readouterr().out
    assert "litellm not loaded at startup" in out
    assert "loading litellm failed" not in out and "Traceback" not in out

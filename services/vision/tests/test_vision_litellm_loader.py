"""The LiteLLM loader heals a half-initialised module and serialises imports."""

import importlib
import sys
import threading
from types import SimpleNamespace
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


def test_loader_drops_the_module_when_the_import_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_import(name: str) -> Any:
        sys.modules["litellm"] = SimpleNamespace()
        msg = "boom"
        raise ImportError(msg)

    monkeypatch.setattr(importlib, "import_module", failing_import)
    with pytest.raises(ImportError):
        llm.load_litellm()
    assert "litellm" not in sys.modules


def test_loader_rejects_an_import_that_stays_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    def partial_import(name: str) -> Any:
        module = SimpleNamespace(acompletion=object())
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

"""WebSocket routes under the API prefix and the uvicorn options of ``run()``.

The kit's middlewares (correlation id, error handler, HTTP metrics) are pure ASGI and pass
``websocket`` scopes through untouched, so a router with a WebSocket route works like any other
router. ``run()`` forwards WebSocket options (ping interval, maximum frame size) to uvicorn and
accepts a server subclass for services that act before uvicorn closes the connections.
"""

from typing import Any

import pytest
import uvicorn
from fastapi import APIRouter, WebSocket
from fastapi.testclient import TestClient
from mowdeck_service_kit import run
from mowdeck_service_kit.app import STARTUP_FAILURE_EXIT_CODE
from mowdeck_service_kit.testing import make_test_app, make_test_settings
from starlette.websockets import WebSocketDisconnect

ws_router = APIRouter(prefix="/echo")


@ws_router.websocket("/ws")
async def echo(websocket: WebSocket) -> None:
    await websocket.accept()
    text = await websocket.receive_text()
    if text == "close":
        await websocket.close(code=4401, reason="bye")
        return
    await websocket.send_text(f"echo:{text}")
    await websocket.close()


def test_kit_websocket_route_is_mounted_under_the_api_prefix() -> None:
    app = make_test_app(routers=[ws_router])
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/echo/ws") as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "echo:hello"
        with client.websocket_connect("/api/v1/echo/ws") as ws:
            ws.send_text("close")
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
            assert closed.value.code == 4401
        # HTTP routes and metrics keep working next to the WebSocket route.
        assert client.get("/healthz").status_code == 200
        metrics = client.get("/metrics").text
        assert "/api/v1/echo/ws" not in metrics  # WebSockets are not counted as HTTP requests


def test_kit_websocket_without_prefix_is_not_found() -> None:
    app = make_test_app(routers=[ws_router])
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/echo/ws") as ws,
    ):
        ws.receive_text()


def test_kit_run_passes_uvicorn_options(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        captured.update(kwargs, app=app)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    settings = make_test_settings(http_port=8124)
    app = make_test_app(settings=settings)
    run(app, settings, ws_ping_interval=15.0, ws_max_size=1024)
    assert captured["ws_ping_interval"] == 15.0
    assert captured["ws_max_size"] == 1024
    assert captured["port"] == 8124


class _RecordingServer(uvicorn.Server):
    instances: list["_RecordingServer"] = []  # noqa: RUF012 - test bookkeeping
    start = True

    def run(self, sockets: Any = None) -> None:
        _RecordingServer.instances.append(self)
        self.started = _RecordingServer.start


def test_kit_run_uses_the_given_server_class() -> None:
    settings = make_test_settings(http_port=8125, shutdown_grace_seconds=4)
    app = make_test_app(settings=settings)
    _RecordingServer.start = True
    run(app, settings, server_class=_RecordingServer, ws_ping_interval=5.0)
    config = _RecordingServer.instances[-1].config
    assert config.app is app
    assert config.port == 8125
    assert config.ws_ping_interval == 5.0
    assert config.timeout_graceful_shutdown == 4
    assert config.log_config is None


def test_kit_run_exits_when_the_server_did_not_start() -> None:
    settings = make_test_settings()
    app = make_test_app(settings=settings)
    _RecordingServer.start = False
    with pytest.raises(SystemExit) as exited:
        run(app, settings, server_class=_RecordingServer)
    assert exited.value.code == STARTUP_FAILURE_EXIT_CODE

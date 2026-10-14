"""The planned mow path: PyMammotion's route mapped, fetched in the background, previewed."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from mammotion_support import NOW, cloud
from mowdeck_contracts.v1 import MowerActivity, StartMow
from mowdeck_control import (
    CommandRejected,
    MowPathSource,
    NotConnected,
    PathPreviewCapable,
    PathUnavailable,
)
from mowdeck_control_mammotion import base as mammotion_base
from mowdeck_control_mammotion import mapping
from mowdeck_control_mammotion.fake import (
    EDGE_LINE_HASH,
    FRONT_HASH,
    SAMPLE_PATH_LINES,
    STRIPE_LINE_HASH,
    FakeMammotionPort,
    PathLine,
    install_mow_path,
    route_hash,
    sample_device,
)
from mowdeck_control_mammotion.port import (
    ClientPort,
    GenerateRouteInformation,
    MowPathFetchError,
    TrackedMowPathSaga,
    TransportType,
    WorkMode,
)

FRONT = str(FRONT_HASH)
EDGE_POINTS = len(SAMPLE_PATH_LINES[0][3])
STRIPE_POINTS = len(SAMPLE_PATH_LINES[1][3])


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


# --- mapping -----------------------------------------------------------------------------------


def test_mow_path_of_follows_the_line_order_and_kinds() -> None:
    device = sample_device()
    install_mow_path(device, SAMPLE_PATH_LINES, packets_per_line=3)
    planned = mapping.mow_path_of(device, planned_at=NOW)
    assert planned is not None
    path = planned.path
    assert planned.line_hashes == (EDGE_LINE_HASH, STRIPE_LINE_HASH)
    assert path.kinds == ["edge", "mow"]
    assert path.polylines[0] == SAMPLE_PATH_LINES[0][3]  # packets joined in path_cur order
    assert path.polylines[1] == SAMPLE_PATH_LINES[1][3]
    assert path.points_total == EDGE_POINTS + STRIPE_POINTS
    assert path.path_hash == str(route_hash(SAMPLE_PATH_LINES))
    assert path.area_ids == [FRONT]
    assert path.progress is None
    assert path.planned_at == NOW


def test_mow_path_of_ignores_old_lines_and_incomplete_transactions() -> None:
    device = sample_device()
    old: list[PathLine] = [(99, 0, FRONT_HASH, [(5.0, 5.0), (6.0, 6.0)])]
    install_mow_path(device, old, transaction_id=1)  # an older route's frames stay cached
    install_mow_path(device, [(STRIPE_LINE_HASH, 0, FRONT_HASH, [(9.0, 9.0)])], transaction_id=3)
    device.map.current_mow_path[3][1].total_frame = 2  # a request still missing a frame
    install_mow_path(device, SAMPLE_PATH_LINES, transaction_id=2)  # the current line list
    planned = mapping.mow_path_of(device, planned_at=NOW)
    assert planned is not None
    assert [len(line) for line in planned.path.polylines] == [EDGE_POINTS, STRIPE_POINTS]
    points = [point for line in planned.path.polylines for point in line]
    assert (5.0, 5.0) not in points
    assert (9.0, 9.0) not in points


def test_mow_path_of_with_a_missing_line() -> None:
    device = sample_device()
    install_mow_path(device, SAMPLE_PATH_LINES)
    del device.map.current_mow_path[1_700_000_000_000][2]  # the stripe frame never came
    device.map.current_mow_path[1_700_000_000_000][1].total_frame = 1
    assert mapping.mow_path_of(device, planned_at=NOW) is None
    partial = mapping.mow_path_of(device, planned_at=NOW, complete_only=False)
    assert partial is not None
    assert partial.line_hashes == (EDGE_LINE_HASH,)
    assert partial.path.kinds == ["edge"]


def test_mow_path_of_without_a_route() -> None:
    assert mapping.mow_path_of(sample_device(), planned_at=NOW) is None


def _running(device_lines: tuple[PathLine, ...] = SAMPLE_PATH_LINES) -> Any:
    device = sample_device()
    install_mow_path(device, device_lines)
    work = device.report_data.work
    work.path_hash = route_hash(device_lines)
    device.report_data.dev.sys_status = int(WorkMode.MODE_WORKING)
    return device


def test_progress_counts_the_lines_before_the_active_one() -> None:
    device = _running()
    planned = mapping.mow_path_of(device, planned_at=NOW)
    assert planned is not None
    work = device.report_data.work
    work.ub_path_hash = STRIPE_LINE_HASH
    work.real_path_num = (3 << 8) | 1  # now_index 3, direction 1
    progress = mapping.path_progress_of(device, planned)
    assert progress is not None
    assert progress.index == EDGE_POINTS + 3
    assert progress.direction == 1
    work.real_path_num = 500 << 8  # clamped to the line
    progress = mapping.path_progress_of(device, planned)
    assert progress is not None
    assert progress.index == EDGE_POINTS + STRIPE_POINTS
    work.ub_path_hash = EDGE_LINE_HASH
    work.real_path_num = 2 << 8
    progress = mapping.path_progress_of(device, planned)
    assert progress is not None
    assert progress.index == 2


def test_progress_is_unknown_for_another_route_or_line_or_without_a_job() -> None:
    device = _running()
    planned = mapping.mow_path_of(device, planned_at=NOW)
    assert planned is not None
    work = device.report_data.work
    work.ub_path_hash = 12345
    assert mapping.path_progress_of(device, planned) is None
    work.ub_path_hash = EDGE_LINE_HASH
    work.path_hash = 7
    assert mapping.path_progress_of(device, planned) is None
    work.path_hash = route_hash(SAMPLE_PATH_LINES)
    device.report_data.dev.sys_status = int(WorkMode.MODE_READY)
    assert mapping.path_progress_of(device, planned) is None


def test_progress_of_a_single_line_route_without_an_active_hash() -> None:
    lines: tuple[PathLine, ...] = (SAMPLE_PATH_LINES[1],)
    device = _running(lines)
    planned = mapping.mow_path_of(device, planned_at=NOW)
    assert planned is not None
    device.report_data.work.ub_path_hash = 0
    device.report_data.work.real_path_num = 4 << 8
    progress = mapping.path_progress_of(device, planned)
    assert progress is not None
    assert progress.index == 4


# --- the running job ---------------------------------------------------------------------------


async def test_start_fetches_the_route_in_the_background_and_tracks_progress() -> None:
    conn, port = cloud()
    assert isinstance(conn, MowPathSource)
    await conn.connect()
    assert conn.current_path() is None
    dispatch = await conn.dispatch(StartMow(area_ids=[FRONT]))
    assert dispatch.confirm is not None
    await _settle()
    assert len(port.path_fetches) == 1
    plan, route = port.path_fetches[0]
    assert plan is False  # skip_planning: the mower is never planned again
    assert route is not None
    assert route.one_hashs == [FRONT_HASH]
    path = conn.current_path()
    assert path is not None
    assert path.path_hash == str(route_hash(SAMPLE_PATH_LINES))
    assert path.progress is not None
    assert path.progress.index == 0  # first line, now_index 0
    port.device().report_data.work.ub_path_hash = STRIPE_LINE_HASH
    port.device().report_data.work.real_path_num = 2 << 8
    path = conn.current_path()
    assert path is not None
    assert path.progress is not None
    assert path.progress.index == EDGE_POINTS + 2
    # more reports do not fetch again
    await port.emit()
    await _settle()
    assert len(port.path_fetches) == 1
    # the job ends: the route is forgotten
    port.set_mode(WorkMode.MODE_READY)
    await port.emit()
    assert conn.current_path() is None
    await conn.disconnect()


async def test_reconnect_during_a_job_fetches_the_route() -> None:
    port = FakeMammotionPort()
    device = port._device
    device.report_data.dev.sys_status = int(WorkMode.MODE_PAUSE)
    device.report_data.work.path_hash = route_hash(SAMPLE_PATH_LINES)
    device.report_data.work.ub_path_hash = EDGE_LINE_HASH
    conn, _ = cloud(port)
    await conn.connect()
    await _settle()
    assert [plan for plan, _ in port.path_fetches] == [False]
    assert port.path_fetches[0][1] is None  # no start of ours: the job's own settings
    path = conn.current_path()
    assert path is not None
    assert path.progress is not None
    await conn.disconnect()


async def test_a_failed_fetch_is_retried_later_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = FakeMammotionPort()
    port.path_error = MowPathFetchError("the mow path fetch failed: SagaFailedError")
    device = port._device
    device.report_data.dev.sys_status = int(WorkMode.MODE_WORKING)
    device.report_data.work.path_hash = route_hash(SAMPLE_PATH_LINES)
    conn, _ = cloud(port)
    await conn.connect()
    await _settle()
    assert len(port.path_fetches) == 1
    await port.emit()
    await _settle()
    assert len(port.path_fetches) == 1  # not again before PATH_RETRY_S
    monkeypatch.setattr(mammotion_base, "PATH_RETRY_S", 0.0)
    for _ in range(5):
        await port.emit()
        await _settle()
    assert len(port.path_fetches) == mammotion_base.PATH_ATTEMPTS
    assert conn.current_path() is None
    await conn.disconnect()


async def test_a_fetch_without_a_route_leaves_the_path_empty() -> None:
    port = FakeMammotionPort()
    conn, _ = cloud(port)
    await conn.connect()
    await conn.dispatch(StartMow(area_ids=[FRONT]))
    port.path_lines = None  # the mower hands back nothing
    port.device().map.root_hash_lists = []
    await _settle()
    assert conn.current_path() is None
    await conn.disconnect()


async def test_disconnect_cancels_a_running_fetch() -> None:
    port = FakeMammotionPort()
    port.path_gate = asyncio.Event()
    conn, _ = cloud(port)
    await conn.connect()
    await conn.dispatch(StartMow(area_ids=[FRONT]))
    await _settle()
    assert len(port.path_fetches) == 1
    await conn.disconnect()  # does not wait for the gate


# --- preview -----------------------------------------------------------------------------------


async def test_preview_plans_without_starting() -> None:
    conn, port = cloud()
    assert isinstance(conn, PathPreviewCapable)
    await conn.connect()
    path = await conn.preview_path([FRONT], {"name": "stripes", "border_laps": 1})
    assert path.progress is None
    assert path.points_total == EDGE_POINTS + STRIPE_POINTS
    assert path.kinds == ["edge", "mow"]
    plan, route = port.path_fetches[-1]
    assert plan is True
    assert isinstance(route, GenerateRouteInformation)
    assert route.one_hashs == [FRONT_HASH]
    assert "start_job" not in port.names()
    assert "generate_route_information" not in port.names()  # the saga sends it
    state = await conn.get_state()
    assert state.activity is MowerActivity.DOCKED
    await conn.disconnect()


@pytest.mark.parametrize(
    ("area_ids", "pattern", "message"),
    [
        (["12345"], None, "unknown area ids"),
        ([], None, "area"),
        ([FRONT], {"name": "spiral"}, "spiral"),
    ],
)
async def test_preview_refuses_bad_areas_and_patterns(
    area_ids: list[str], pattern: dict[str, Any] | None, message: str
) -> None:
    conn, port = cloud()
    await conn.connect()
    with pytest.raises(CommandRejected, match=message):
        await conn.preview_path(area_ids, pattern)
    assert port.path_fetches == []
    await conn.disconnect()


async def test_preview_refused_while_a_job_runs() -> None:
    conn, port = cloud()
    await conn.connect()
    port.set_mode(WorkMode.MODE_WORKING)
    port.device().report_data.dev.charge_state = 0
    with pytest.raises(CommandRejected, match="mowing"):
        await conn.preview_path([FRONT], None)
    await conn.disconnect()


async def test_preview_failures() -> None:
    conn, port = cloud()
    await conn.connect()
    port.path_error = MowPathFetchError("the mow path fetch over the cloud is switched off")
    with pytest.raises(PathUnavailable, match="switched off") as caught:
        await conn.preview_path([FRONT], None)
    assert not caught.value.timed_out
    port.path_error = None
    port.path_lines = None
    with pytest.raises(PathUnavailable, match="no route"):
        await conn.preview_path([FRONT], None)
    port.link = False
    with pytest.raises(NotConnected):
        await conn.preview_path([FRONT], None)
    await conn.disconnect()


async def test_preview_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mammotion_base, "PATH_PREVIEW_TIMEOUT_S", 0.01)
    conn, port = cloud()
    await conn.connect()
    port.path_gate = asyncio.Event()
    with pytest.raises(PathUnavailable, match="in time") as caught:
        await conn.preview_path([FRONT], None)
    assert caught.value.timed_out
    await conn.disconnect()


# --- ClientPort.fetch_mow_path -----------------------------------------------------------------


class _PathHandle:
    def __init__(self, *, enabled: bool = True, ble: bool = False) -> None:
        self.device_name = "Luba-VP1"
        self.snapshot = SimpleNamespace(raw=sample_device("Luba-VP1"))
        self.mow_path_fetch_enabled = enabled
        self.ble = ble
        self.commands = object()
        self.sagas: list[TrackedMowPathSaga] = []
        self.finish: BaseException | bool | None = None
        """None: the saga never ends; True: it ends well; an exception: it fails with it."""

    def is_transport_connected(self, kind: TransportType) -> bool:
        return self.ble and kind is TransportType.BLE

    async def send_raw(self, payload: bytes) -> None:  # pragma: no cover - never called
        return None

    async def enqueue_saga(self, saga: TrackedMowPathSaga) -> None:
        self.sagas.append(saga)
        if self.finish is True:
            saga.confirmed_hash = 42
            saga.finished.set()
        elif isinstance(self.finish, BaseException):
            saga.error = self.finish
            saga.finished.set()


def _client_port(handle: _PathHandle) -> ClientPort:
    async def opener() -> Any:
        return SimpleNamespace(reauth_required=None), handle

    return ClientPort(opener)


async def test_client_port_runs_a_tracked_saga() -> None:
    handle = _PathHandle()
    handle.finish = True
    port = _client_port(handle)
    await port.open()
    route = GenerateRouteInformation(one_hashs=[FRONT_HASH])
    assert await port.fetch_mow_path(route, plan=True, timeout_s=5) == 42
    saga = handle.sagas[-1]
    assert saga.total_timeout == 90.0
    assert saga._skip_planning is False
    assert saga._sync_type == 3
    assert await port.fetch_mow_path(None, plan=False, timeout_s=5) is None
    assert handle.sagas[-1]._skip_planning is True
    with pytest.raises(ValueError, match="needs a route"):
        await port.fetch_mow_path(None, plan=True, timeout_s=5)


async def test_client_port_fetch_gate_failure_and_timeout() -> None:
    handle = _PathHandle(enabled=False)
    port = _client_port(handle)
    await port.open()
    with pytest.raises(MowPathFetchError, match="switched off"):
        await port.fetch_mow_path(None, plan=False, timeout_s=5)
    handle.mow_path_fetch_enabled = True
    handle.finish = RuntimeError("boom")
    with pytest.raises(MowPathFetchError, match="RuntimeError"):
        await port.fetch_mow_path(None, plan=False, timeout_s=5)
    handle.finish = None
    with pytest.raises(TimeoutError, match="did not arrive"):
        await port.fetch_mow_path(None, plan=False, timeout_s=0.01)
    ble_handle = _PathHandle(enabled=False, ble=True)  # BLE fetches always run
    ble_handle.finish = True
    ble_port = _client_port(ble_handle)
    await ble_port.open()
    await ble_port.fetch_mow_path(None, plan=False, timeout_s=5)
    assert ble_handle.sagas[-1]._sync_type == 2


async def test_tracked_saga_reports_its_end_and_the_confirmed_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saga = TrackedMowPathSaga(
        command_builder=object(),
        send_command=lambda _payload: asyncio.sleep(0),
        get_map=lambda: sample_device().map,
        zone_hashs=[FRONT_HASH],
    )

    async def failing(self: object, _broker: object) -> None:
        raise RuntimeError("no device")

    monkeypatch.setattr("pymammotion.messaging.mow_path_saga.MowPathSaga.execute", failing)
    with pytest.raises(RuntimeError):
        await saga.execute(object())
    assert saga.finished.is_set()
    assert isinstance(saga.error, RuntimeError)

    async def planned(self: Any, _broker: object) -> None:
        self._route_val = SimpleNamespace(path_hash=77, sub_cmd=0)

    monkeypatch.setattr("pymammotion.messaging.mow_path_saga.MowPathSaga._plan_route", planned)
    await saga._plan_route(object())
    assert saga.confirmed_hash == 77

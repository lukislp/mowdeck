"""The demo CLI runs the simulator without the platform."""

import io

import pytest
from mowdeck_control import demo


async def test_run_demo_prints_state_lines_without_sleeping() -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    out = io.StringIO()
    code = await demo.run_demo(
        seconds=2, interval=0.5, time_scale=60, areas=["front"], out=out, sleep=fake_sleep
    )
    lines = out.getvalue().splitlines()
    assert code == 0
    assert slept == [0.5] * 4
    assert lines[0] == "start_mow executed: mowing front with stripes"
    assert len(lines) == 6
    assert "activity=mowing" in lines[-1]
    assert "blade=on" in lines[-1]
    assert "progress=" in lines[-1]


async def test_run_demo_unknown_area_fails() -> None:
    out = io.StringIO()
    code = await demo.run_demo(seconds=1, interval=0.5, time_scale=1, areas=["pond"], out=out)
    assert code == 1
    assert "rejected" in out.getvalue()


def test_main_runs_for_zero_seconds() -> None:
    out = io.StringIO()
    assert demo.main(["--seconds", "0", "--areas", "back"], out=out) == 0
    lines = out.getvalue().splitlines()
    assert lines[0] == "start_mow executed: mowing back with stripes"
    assert lines[1].startswith("t=   0.0s activity=mowing")


def test_main_rejects_bad_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        demo.main(["--interval", "0"])
    assert "--interval" in capsys.readouterr().err

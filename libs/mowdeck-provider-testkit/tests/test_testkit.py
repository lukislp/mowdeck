"""The kit itself: it passes a conforming adapter and catches each broken contract.

A check that stops catching its violation would let a broken provider through, so every check
that can be broken by an adapter has a negative test here. The full suite against real adapters
runs in their own packages (``libs/mowdeck-control-*/tests/test_*_conformance.py``) and against
the rendered provider template (``tools/repo-checks/tests/test_provider_template.py``).
"""

import subprocess
import sys
import textwrap
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from mowdeck_contracts.v1 import SetSetting
from mowdeck_control import ConnectionConfig, MowerConnection, SettingsCapable
from mowdeck_control.simulator import create_simulated_connection
from mowdeck_provider_testkit import (
    MowerUnderTest,
    ProviderUnderTest,
    checks,
    installed,
    open_mower,
)
from testkit_support import Breakage, FakeMower, FakeSettingsMower

CORE = installed("mowdeck-control")
"""A distribution with entry points in every group but no adapter: enough for the harness."""

Check = Callable[[MowerUnderTest], Awaitable[object]]


def _subject(
    factory: Callable[[ConnectionConfig], MowerConnection], **extra: Any
) -> ProviderUnderTest:
    return ProviderUnderTest(distribution="mowdeck-control", connection_factory=factory, **extra)


@asynccontextmanager
async def _fake(breakage: Breakage | None = None, **extra: Any) -> AsyncIterator[MowerUnderTest]:
    subject = _subject(lambda config: FakeMower(config, breakage), **extra)
    async with open_mower(subject, CORE) as mower:
        yield mower


CONNECTION_CHECKS: list[Check] = [
    checks.check_connection_lifecycle,
    checks.check_capabilities_and_state,
    checks.check_mower_can_be_started,
    checks.check_base_job_flow,
    checks.check_undeclared_commands_refused,
    checks.check_declared_commands_accepted,
    checks.check_start_plans,
    checks.check_manual_drive,
    checks.check_emergency_stop_at_rest,
    checks.check_emergency_stop_while_busy,
]


@pytest.mark.parametrize("check", CONNECTION_CHECKS, ids=lambda c: c.__name__)
async def test_a_conforming_minimal_adapter_passes(check: Check) -> None:
    async with _fake() as mower:
        await check(mower)


@pytest.mark.parametrize(
    "check",
    [
        *CONNECTION_CHECKS,
        checks.check_settings_round_trip,
        checks.check_native_pattern_start,
        checks.check_keep_job_on_return,
    ],
    ids=lambda c: c.__name__,
)
async def test_the_simulator_passes(check: Check) -> None:
    subject = _subject(create_simulated_connection)
    async with open_mower(subject, CORE) as mower:
        await check(mower)


async def test_settings_round_trip_passes_a_confirming_adapter() -> None:
    subject = _subject(FakeSettingsMower)
    async with open_mower(subject, CORE) as mower:
        assert await checks.check_settings_round_trip(mower) == ["rain_detection"]
        await checks.check_capabilities_and_state(mower)


@pytest.mark.parametrize(
    ("breakage", "check", "message"),
    [
        (
            Breakage(light_declared_but_refused=True),
            checks.check_declared_commands_accepted,
            "light",
        ),
        (
            Breakage(settings_without_protocol=True),
            checks.check_capabilities_and_state,
            "SettingsCapable",
        ),
        (
            Breakage(heartbeat_from_adapter=True),
            checks.check_capabilities_and_state,
            "heartbeat_at",
        ),
        (
            Breakage(wrong_kind_declared=True),
            checks.check_capabilities_and_state,
            "connection_kinds",
        ),
        (
            Breakage(drive_session_not_implemented=True),
            checks.check_manual_drive,
            "NotImplementedError",
        ),
        (Breakage(no_start_offered=True), checks.check_mower_can_be_started, "start_plans"),
        (Breakage(any_plan_accepted=True), checks.check_start_plans, "unknown start plan"),
        (Breakage(watch_never_ends=True), checks.check_connection_lifecycle, "disconnect"),
        (
            Breakage(slow_emergency_stop=True),
            checks.check_emergency_stop_while_busy,
            "did not return",
        ),
    ],
    ids=lambda value: value.__name__ if callable(value) else None,
)
async def test_each_broken_contract_is_caught(
    breakage: Breakage, check: Check, message: str
) -> None:
    async with _fake(breakage, emergency_stop_timeout_s=0.2) as mower:
        with pytest.raises(AssertionError, match=message):
            await check(mower)


async def test_an_executed_setting_the_state_never_shows_is_caught() -> None:
    subject = _subject(lambda config: FakeSettingsMower(config, confirms=False))
    async with open_mower(subject, CORE) as mower:
        with pytest.raises(AssertionError, match="does not show it"):
            await checks.check_settings_round_trip(mower)


async def test_undeclared_commands_never_reach_the_adapter() -> None:
    async with _fake() as mower:
        await checks.check_undeclared_commands_refused(mower)
        assert mower.calls == []  # the spy saw nothing: the controller refused everything
        connection = mower.connection
        assert isinstance(connection, FakeMower) and connection.executed == []


async def test_the_spy_keeps_the_optional_protocols() -> None:
    subject = _subject(FakeSettingsMower)
    async with open_mower(subject, CORE) as mower:
        assert isinstance(mower.connection, SettingsCapable)
        assert mower.calls is not None
        await mower.send(SetSetting(key="rain_detection", value=True))
        assert mower.calls == ["set_setting"]


def test_package_checks_on_the_core_library() -> None:
    checks.check_entry_points_declared(CORE)
    checks.check_entry_points_load(CORE)
    checks.check_no_other_adapter_imported(CORE)
    checks.check_lexicon(CORE)
    assert checks.check_lexicon_covers(CORE, ["stuck", "lifted"]) == 2
    with pytest.raises(AssertionError, match="mowdeck-control-<vendor>"):
        checks.check_package_layout(CORE)
    with pytest.raises(AssertionError, match="does not explain"):
        checks.check_lexicon_covers(CORE, ["stuck", "no-such-code"])


def test_known_violations_are_strict_xfails(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nasyncio_mode = "auto"\n', encoding="utf-8"
    )
    (tmp_path / "test_core.py").write_text(
        textwrap.dedent(
            """
            from mowdeck_provider_testkit import ProviderConformance, ProviderUnderTest

            class TestCore(ProviderConformance):
                subject = ProviderUnderTest(
                    distribution="mowdeck-control",
                    known_violations={
                        "test_package_layout": "the core is no adapter",
                        "test_lexicon": "fixed already",
                    },
                )
            """
        ),
        encoding="utf-8",
    )
    done = subprocess.run(  # noqa: S603 - this interpreter, a generated test file
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-k",
            "test_package_layout or test_lexicon",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
        timeout=120,
    )
    # The real violation is an expected failure; the check that passes although it is listed
    # fails the run (strict), so a fixed violation cannot stay listed.
    assert "1 failed" in done.stdout and "1 xfailed" in done.stdout, done.stdout + done.stderr
    assert "XPASS(strict)" in done.stdout


def test_the_subject_names_its_module() -> None:
    subject = ProviderUnderTest(distribution="mowdeck-control-acme")
    assert subject.module == "mowdeck_control_acme"
    assert replace(subject, required_locales=("en",)).required_locales == ("en",)

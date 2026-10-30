"""The conformance checks, as plain functions that raise ``AssertionError``.

Every check is derived from an existing contract; the docstring names its source. Where a
contract leaves room, the assumption is written down here and in ADR 0069. The pytest suite
(:mod:`mowdeck_provider_testkit.suite`) calls them; they can also be called directly.
"""

import asyncio
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator, Mapping
from contextlib import suppress
from typing import Any, Final

from mowdeck_contracts.v1 import (
    PROVIDER_ID_PATTERN,
    SETTING_KEYS,
    BaseStationFirmwareUpdate,
    CameraAccess,
    CommandResult,
    CommandStatus,
    ConnectionKind,
    DriveCalibrate,
    EmergencyStop,
    FirmwareUpdate,
    LeaveDock,
    ManualDrive,
    MowerActivity,
    MowerCapabilities,
    MowerCommand,
    MowerErrorCategory,
    MowerMap,
    MowerState,
    PauseMow,
    ProviderDescriptor,
    RelocateDock,
    RestartMower,
    ResumeMow,
    ResyncRtk,
    ReturnToDock,
    SetBladeHeight,
    SetDriveSpeed,
    SetLight,
    SetSetting,
    SetSpeed,
    SettingSpec,
    SettingType,
    SettingValue,
    StartMow,
    SyncMap,
    TimeWindow,
)
from mowdeck_control import (
    BaseStationPort,
    BridgeScanCapable,
    CameraCapable,
    CloudLoginRefused,
    CommandRejected,
    ConnectionRegistry,
    DriveSpeedCapable,
    ErrorLexiconProvider,
    ErrorLexiconRegistry,
    FirmwarePort,
    MaintenanceCapable,
    MowerConnection,
    ProviderContext,
    SettingsCapable,
    UndockCapable,
)

from mowdeck_provider_testkit.harness import (
    OK,
    MowerUnderTest,
    open_mower,
    provider_specs,
    wrong_credentials,
)
from mowdeck_provider_testkit.package import (
    ADAPTER_PREFIX,
    CLOUD_PROVIDERS,
    CONNECTIONS,
    ERROR_LEXICONS,
    GROUPS,
    LINK_GROUPS,
    SDK_FREE_IMPORT,
    InstalledPackage,
    foreign_adapter_imports,
    target_module,
)
from mowdeck_provider_testkit.subject import ProviderUnderTest

GATEWAY_OWNED_FIELDS: Final = ("heartbeat_at", "cloud_provider")
"""``MowerState`` fields the Device-Gateway sets, never an adapter."""
SENSOR_STOPS: Final = frozenset({MowerErrorCategory.LIFT_TILT, MowerErrorCategory.COLLISION})
"""Safety sensor stops: never ``drive_recoverable`` (ADR 0012, ``MowerState.drive_recoverable``)."""
UNKNOWN_PLAN: Final = "conformance_unknown_plan"
"""A start plan reference no adapter offers."""
UNKNOWN_AREA: Final = "conformance-no-such-area"


def expect(condition: object, message: str) -> None:
    """Fail the check with ``message`` unless ``condition`` holds."""
    if not condition:
        raise AssertionError(message)


def describe(result: CommandResult) -> str:
    """``status (reason)`` of a command result."""
    return f"{result.status.value} ({result.reason or 'no reason'})"


# --- The package ------------------------------------------------------------------------------


def check_package_layout(package: InstalledPackage) -> None:
    """ADR 0066 ("How to add a vendor", decision 6): an adapter is a package
    ``mowdeck-control-<vendor>`` whose import name follows the distribution name, so the
    repository's vendor isolation test recognises it as an adapter."""
    expect(
        package.name.startswith(ADAPTER_PREFIX) and len(package.name) > len(ADAPTER_PREFIX),
        f"{package.name}: an adapter package is named {ADAPTER_PREFIX}<vendor>",
    )
    expect(
        package.package_dir() is not None,
        f"{package.name}: the import package {package.module} is not installed",
    )


def check_entry_points_declared(package: InstalledPackage) -> None:
    """ADR 0066 decision 6 and ADR 0067: an adapter registers something that reaches mowers
    (``mowdeck.connections`` or ``mowdeck.cloud_providers``), ships an error lexicon
    (``mowdeck.error_lexicons``), and every entry point points into its own package."""
    expect(
        any(package.entry_points(group) for group in LINK_GROUPS),
        f"{package.name} registers nothing under {' or '.join(LINK_GROUPS)}",
    )
    expect(
        package.entry_points(ERROR_LEXICONS),
        f"{package.name} registers no error lexicon under {ERROR_LEXICONS}",
    )
    for group in GROUPS:
        for entry_point in package.entry_points(group):
            expect(
                target_module(entry_point) == package.module,
                f"{group} {entry_point.name} -> {entry_point.value} points outside "
                f"{package.module}",
            )


def check_entry_points_load(package: InstalledPackage) -> None:
    """ADR 0012, 0066, 0067: every entry point loads through ``mowdeck_control``'s own loader
    (the one the Device-Gateway uses) and names what it registers consistently: a connection
    factory under a known ``ConnectionKind``, a ``ProviderSpec`` whose ``provider`` is the entry
    point name, an error lexicon whose ``vendor`` is the entry point name. The loaders log and
    skip a broken entry point, so a missing name here is a broken registration."""
    connections = {ep.name for ep in package.entry_points(CONNECTIONS)}
    if connections:
        loaded = ConnectionRegistry(package.source(CONNECTIONS)).load_entry_points()
        expect(
            {str(kind) for kind in loaded} == connections,
            f"connection entry points that do not load: {sorted(connections - set(loaded))}",
        )
    providers = {ep.name for ep in package.entry_points(CLOUD_PROVIDERS)}
    if providers:
        specs = provider_specs(package)
        expect(
            set(specs) == providers,
            f"cloud provider entry points that do not load: {sorted(providers - set(specs))}",
        )
    lexicons = {ep.name for ep in package.entry_points(ERROR_LEXICONS)}
    registry = ErrorLexiconRegistry(package.source(ERROR_LEXICONS))
    loaded_lexicons = set(registry.load_entry_points())
    expect(
        loaded_lexicons == lexicons,
        f"error lexicon entry points that do not load: {sorted(lexicons - loaded_lexicons)}",
    )


def check_vendor_modules_declared(package: InstalledPackage) -> None:
    """ADR 0066 decision 6: ``[tool.mowdeck.adapter] vendor-modules`` names the SDK modules
    the package wraps (the core must never import them), ``brand-names`` the words that name
    the vendor. Only checkable from a source tree (the workspace installs editable)."""
    table = package.adapter_table()
    if table is None:
        return
    modules = table.get("vendor-modules", [])
    expect(
        isinstance(modules, list) and modules and all(isinstance(m, str) for m in modules),
        f"{package.name}: declare [tool.mowdeck.adapter] vendor-modules (the SDK modules)",
    )
    expect(
        package.module not in modules,
        f"{package.name}: vendor-modules lists the adapter itself; list the vendor SDK",
    )
    brands = table.get("brand-names", [])
    expect(
        isinstance(brands, list) and all(isinstance(b, str) for b in brands),
        f"{package.name}: [tool.mowdeck.adapter] brand-names must be a list of words",
    )


def check_no_other_adapter_imported(package: InstalledPackage) -> None:
    """ADR 0066 decision 6: an adapter never imports another adapter (each vendor is a
    separate package, GPL code stays isolated)."""
    found = foreign_adapter_imports(package)
    expect(not found, f"{package.name} imports other adapter packages: {found}")


def check_specs_import_without_the_vendor_sdk(package: InstalledPackage) -> None:
    """``mowdeck_control.providers``: the spec "should be importable without the vendor SDK
    (import it inside ``create``), so a disabled provider costs nothing" (the gateway loads every
    installed spec, enabled or not). Checked in a fresh interpreter with the declared
    ``vendor-modules`` blocked."""
    targets = [ep.value for ep in package.entry_points(CLOUD_PROVIDERS)]
    table = package.adapter_table()
    blocked = list(table.get("vendor-modules", [])) if table else []
    if not targets or not blocked:
        return
    done = subprocess.run(  # noqa: S603 - this interpreter, the kit's own script
        [sys.executable, "-c", SDK_FREE_IMPORT, json.dumps(blocked), json.dumps(targets)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    expect(
        done.returncode == 0,
        f"the cloud provider spec of {package.name} needs the vendor SDK at import time "
        f"(import it inside ProviderSpec.create):\n{done.stderr.strip()[-2000:]}",
    )
    loaded = json.loads(done.stdout.strip().splitlines()[-1])
    expect(not loaded, f"importing the spec loaded vendor modules: {loaded}")


# --- The cloud provider -----------------------------------------------------------------------


def missing_texts(descriptor: ProviderDescriptor, locales: Iterable[str]) -> list[str]:
    """``where: locale`` of every user-visible descriptor text without that locale."""
    return [
        f"{where}: {locale}"
        for where, text in descriptor.texts()
        for locale in locales
        if not text.get(locale, "").strip()
    ]


async def check_provider_descriptor(package: InstalledPackage, subject: ProviderUnderTest) -> None:
    """ADR 0066 decisions 1 and 2 and the amendment on localized texts: the spec's provider id
    is valid and equals the descriptor's; only a provider tested on real hardware is on by
    default; the descriptor is a valid contract (round trip through JSON); every user-visible
    text exists in the app's languages; the fake credentials fit the descriptor's fields."""
    for provider, spec in sorted(provider_specs(package).items()):
        expect(re.fullmatch(PROVIDER_ID_PATTERN, provider), f"invalid provider id {provider!r}")
        plugin = spec.create(ProviderContext(fake=True))
        try:
            descriptor = plugin.descriptor
            expect(
                descriptor.provider == provider,
                f"{provider}: the descriptor names provider {descriptor.provider!r}",
            )
            expect(
                not spec.enabled_by_default or descriptor.tested_on_hardware,
                f"{provider}: enabled_by_default is only for providers tested on real hardware",
            )
            again = ProviderDescriptor.model_validate_json(descriptor.model_dump_json())
            expect(again == descriptor, f"{provider}: the descriptor does not survive JSON")
            missing = missing_texts(descriptor, subject.required_locales)
            expect(not missing, f"{provider} lacks texts:\n" + "\n".join(missing))
            if subject.fake_credentials is not None:
                problems = descriptor.problems(descriptor.normalized(subject.fake_credentials))
                expect(
                    not problems,
                    f"{provider}: the fake credentials do not fit the descriptor: "
                    + ", ".join(f"{p.key} {p.code}" for p in problems),
                )
        finally:
            await plugin.close()


async def check_provider_discovery(package: InstalledPackage, subject: ProviderUnderTest) -> None:
    """``CloudProviderPlugin.discover`` (ADR 0066 decision 1): the fake account lists at least
    one mower, and refused credentials raise ``CloudLoginRefused`` whose message holds no
    secret (``CloudLoginRefused``: "The message never holds them"). Assumption: the fake cloud
    refuses a changed secret like the vendor would."""
    credentials = subject.fake_credentials
    if credentials is None:
        return
    for provider, spec in sorted(provider_specs(package).items()):
        plugin = spec.create(ProviderContext(fake=True))
        try:
            normalized = plugin.descriptor.normalized(credentials)
            found = await asyncio.wait_for(plugin.discover(normalized), subject.command_timeout_s)
            expect(
                any(device.is_mower for device in found.devices),
                f"{provider}: the fake account lists no mower",
            )
        finally:
            await plugin.close()
        refused = spec.create(ProviderContext(fake=True))
        try:
            secrets = refused.descriptor.secret_keys
            wrong = wrong_credentials(normalized, secrets)
            try:
                await asyncio.wait_for(refused.discover(wrong), subject.command_timeout_s)
            except CloudLoginRefused as exc:
                leaked = [k for k in secrets if wrong.get(k) and wrong[k] in str(exc)]
                expect(not leaked, f"{provider}: CloudLoginRefused repeats the secret {leaked}")
            else:
                raise AssertionError(f"{provider}: a wrong secret was not refused")
        finally:
            await refused.close()


# --- The connection ---------------------------------------------------------------------------


def capability_protocols(caps: MowerCapabilities) -> Iterator[tuple[str, type, bool]]:
    """``(declaration, protocol, declared)`` for every optional capability that needs a
    protocol (``mowdeck_control.connection``: "declares X and implements Y")."""
    maintenance = set(caps.maintenance) - {"leave_dock"}
    yield "settings", SettingsCapable, bool(caps.settings)
    yield "maintenance", MaintenanceCapable, bool(maintenance)
    yield "undock", UndockCapable, caps.undock
    yield "firmware_update", FirmwarePort, caps.firmware_update
    yield "base_station_firmware_update", BaseStationPort, caps.base_station_firmware_update
    yield "drive_speed_setting", DriveSpeedCapable, caps.drive_speed_setting is not None
    yield "camera", CameraCapable, caps.camera is not CameraAccess.NONE
    yield "links_supported: ble_bridge", BridgeScanCapable, "ble_bridge" in caps.links_supported


async def check_connection_lifecycle(mower: MowerUnderTest) -> None:
    """``MowerConnection`` (ADR 0012): ``is_connected`` is True only between ``connect`` and
    ``disconnect``, both are idempotent, ``watch_state`` yields the current state first and
    every iterator ends on ``disconnect``."""
    connection = mower.connection
    timeout = mower.subject.command_timeout_s
    expect(isinstance(connection, MowerConnection), "the connection is no MowerConnection")
    expect(isinstance(connection.kind, ConnectionKind), "kind is no ConnectionKind")
    expect(not connection.is_connected, "is_connected before connect()")
    await asyncio.wait_for(connection.connect(), timeout)
    await asyncio.wait_for(connection.connect(), timeout)
    expect(connection.is_connected, "is_connected is False after connect()")
    state = await connection.get_state()
    watch = connection.watch_state()
    first = await asyncio.wait_for(anext(watch), timeout)
    expect(isinstance(first, MowerState), "watch_state() yielded no MowerState")
    expect(
        first.mower_id == state.mower_id,
        "watch_state() does not start with the current state of this mower",
    )
    await asyncio.wait_for(connection.disconnect(), timeout)
    expect(not connection.is_connected, "is_connected is True after disconnect()")
    try:
        while True:  # queued states drain first, then the iterator ends
            await asyncio.wait_for(anext(watch), 5.0)
    except StopAsyncIteration:
        ended = True
    except TimeoutError:
        ended = False
    expect(ended, "watch_state() iterators do not end on disconnect()")
    await asyncio.wait_for(connection.disconnect(), timeout)


def state_problems(
    state: MowerState,
    *,
    mower: MowerUnderTest,
    caps: MowerCapabilities,
    lexicons: Iterable[ErrorLexiconProvider] = (),
) -> list[str]:
    """What is wrong with one state the adapter reported (ADR 0012, 0046, 0067 and the field
    descriptions of ``MowerState``)."""
    problems: list[str] = []
    config, kind = mower.config, mower.connection.kind
    if state.mower_id != config.mower_id or state.account_id != config.account_id:
        problems.append("mower_id or account_id differ from the connection config")
    if state.connection != kind:
        problems.append(f"connection is {state.connection}, the adapter's kind is {kind}")
    for name in GATEWAY_OWNED_FIELDS:
        if getattr(state, name) is not None:
            problems.append(f"{name} is set by the Device-Gateway, never by an adapter")
    if not state.connected:
        problems.append("connected=False is set by the Device-Gateway, never by an adapter")
    if state.observed_at.tzinfo is None:
        problems.append("observed_at is not timezone-aware")
    if state.drive_recoverable and state.activity is not MowerActivity.ERROR:
        problems.append("drive_recoverable outside activity error")
    if state.drive_recoverable and state.error_category in SENSOR_STOPS:
        problems.append(f"a {state.error_category} sensor stop is marked drive_recoverable")
    if state.error_category is not None and state.activity is not MowerActivity.ERROR:
        problems.append("error_category is set outside activity error")
    declared = {spec.key: spec for spec in caps.settings}
    for key, value in state.settings.items():
        spec = declared.get(key)
        if spec is None:
            problems.append(f"settings reports {key!r}, which the capabilities do not declare")
        elif (problem := spec.value_problem(value)) is not None:
            problems.append(f"settings: {problem}")
    if state.error_code and state.error_category is not None:
        for provider in lexicons:
            entry = provider.entry(state.error_code)
            if entry is not None and entry.category is not state.error_category:
                problems.append(
                    f"error {state.error_code}: category {state.error_category}, the lexicon "
                    f"says {entry.category}"
                )
    return problems


async def check_capabilities_and_state(
    mower: MowerUnderTest, lexicons: Iterable[ErrorLexiconProvider] = ()
) -> None:
    """``MowerCapabilities`` and ``MowerState`` are well-formed and consistent: the connection's
    own kind is among ``connection_kinds``, every declared optional feature comes with its
    protocol, the state belongs to this mower and fills no gateway-owned field, settings are
    declared ones with valid values, and an error's category agrees with the lexicon."""
    controller = await mower.start()
    caps = await controller.capabilities(refresh=True)
    again = MowerCapabilities.model_validate_json(caps.model_dump_json())
    expect(again == caps, "the capabilities do not survive JSON")
    kind = mower.connection.kind
    expect(
        kind in caps.connection_kinds,
        f"connection_kinds {sorted(caps.connection_kinds)} misses the connection's kind {kind}",
    )
    missing = [
        f"{declaration} needs {protocol.__name__}"
        for declaration, protocol, declared in capability_protocols(caps)
        if declared and not isinstance(mower.connection, protocol)
    ]
    expect(not missing, "declared but not implemented: " + "; ".join(missing))
    state = await mower.connection.get_state()
    problems = state_problems(state, mower=mower, caps=caps, lexicons=lexicons)
    expect(not problems, "state problems: " + "; ".join(problems))


def start_command(caps: MowerCapabilities, mower_map: MowerMap) -> StartMow | None:
    """How a client starts this mower: its first map area, else its first start plan (ADR 0066
    amendment); None when a client could offer no start at all."""
    if mower_map.areas:
        return StartMow(area_ids=[mower_map.areas[0].id])
    if caps.start_plans:
        return StartMow(plan_ref=caps.start_plans[0].ref)
    return None


async def check_mower_can_be_started(mower: MowerUnderTest) -> None:
    """ADR 0066 amendment "starts without map areas": a client starts a mower by tapping map
    areas or through ``MowerCapabilities.start_plans``. A connection with neither leaves no
    start a client could offer, although every adapter supports ``StartMow``."""
    controller = await mower.start()
    caps = await controller.capabilities(refresh=True)
    mower_map = await controller.mower_map(refresh=True)
    expect(
        start_command(caps, mower_map) is not None,
        "the map has no areas and the capabilities declare no start_plans: no client can "
        "start this mower (declare the starts the vendor offers as start_plans)",
    )


async def _settle(mower: MowerUnderTest, seconds: float = 0.2) -> None:
    """Let the fake client push the state that follows a command."""
    await asyncio.sleep(seconds)
    del mower


async def check_base_job_flow(
    mower: MowerUnderTest, lexicons: Iterable[ErrorLexiconProvider] = ()
) -> None:
    """The commands every adapter supports without a capability flag (ADR 0012 interface; ADR
    0066: "a minimal adapter that only connects, reports its state and starts, pauses and docks
    the mower"): a start as a client sends it, pause, resume, the emergency stop while mowing,
    return to dock with the mower's own job behaviour and with ``end_job``. Each is executed or
    accepted on the fake mower, and every state on the way is well-formed. Assumption: the fake
    mower is idle or docked and online when the check begins."""
    controller = await mower.start()
    caps = await controller.capabilities(refresh=True)
    start = start_command(caps, await controller.mower_map())
    if start is None:
        raise AssertionError("no start a client could send (see check_mower_can_be_started)")
    flow: list[MowerCommand] = [
        start,
        PauseMow(),
        ResumeMow(),
        EmergencyStop(),
        ReturnToDock(),
        ReturnToDock(end_job=True),
    ]
    for command in flow:
        result = await mower.send(command)
        expect(result.status in OK, f"{command.kind} {_args(command)}: {describe(result)}")
        await _settle(mower)
        state = await mower.connection.get_state()
        problems = state_problems(state, mower=mower, caps=caps, lexicons=lexicons)
        expect(not problems, f"state after {command.kind}: " + "; ".join(problems))


def _args(command: MowerCommand) -> str:
    data = command.model_dump(exclude={"kind"}, exclude_defaults=True, mode="json")
    return json.dumps(data, sort_keys=True) if data else ""


async def check_emergency_stop_at_rest(mower: MowerUnderTest) -> None:
    """ADR 0012: the emergency stop has priority and is never refused by the rules; on a mower
    at rest it is executed or accepted too (assumption: a stop that has nothing to stop is not a
    refusal)."""
    result = await mower.send(EmergencyStop())
    expect(result.status in OK, f"emergency_stop at rest: {describe(result)}")


async def check_emergency_stop_while_busy(mower: MowerUnderTest) -> None:
    """``MowerConnection`` rule: "``execute(EmergencyStop())`` may be called while another call
    is still running and must act immediately". The kit starts the mower and, while that call
    is still in flight, sends the stop straight to the connection; it must return executed or
    accepted within ``emergency_stop_timeout_s``."""
    controller = await mower.start()
    caps = await controller.capabilities()
    start = start_command(caps, await controller.mower_map())
    if start is None:
        raise AssertionError("no start a client could send (see check_mower_can_be_started)")
    busy = asyncio.create_task(mower.connection.execute(start))
    await asyncio.sleep(0)
    try:
        outcome = await asyncio.wait_for(
            mower.connection.execute(EmergencyStop()), mower.subject.emergency_stop_timeout_s
        )
    except TimeoutError:
        raise AssertionError(
            "the emergency stop did not return within "
            f"{mower.subject.emergency_stop_timeout_s} s while a start was in flight"
        ) from None
    finally:
        with suppress(Exception):
            await asyncio.wait_for(busy, mower.subject.command_timeout_s)
        if not busy.done():
            busy.cancel()
    expect(
        outcome.status in OK,
        f"emergency_stop during a start: {outcome.status.value} ({outcome.reason})",
    )


def _offline_mower(device: Any) -> bool:
    return bool(device.is_mower and device.online is False)


async def check_emergency_stop_offline(
    package: InstalledPackage, subject: ProviderUnderTest
) -> bool:
    """ADR 0012 priority and the hardware checklist of ``docs/ops/vendor-lib-updates.md`` ("the
    emergency stop is still sent" while the cloud reports the mower offline): on a mower the
    fake account lists as offline, the emergency stop still reaches the adapter, which sends it.
    Assumption: the vendor may then answer that the mower is offline, so any result with a
    reason passes; what must not happen is a stop that is never tried. Returns False when the
    fake account has no offline mower or the connection cannot connect to it."""
    if subject.fake_credentials is None or not provider_specs(package):
        return False
    async with open_mower(subject, package, device_filter=_offline_mower) as mower:
        try:
            await mower.start()
        except Exception:
            return False
        before = len(mower.calls) if mower.calls is not None else 0
        result = await mower.send(EmergencyStop())
        expect(
            result.status in OK or result.reason,
            f"emergency_stop on an offline mower: {describe(result)} without a reason",
        )
        if mower.calls is not None:
            tried = [c for c in mower.calls[before:] if c.endswith(":emergency_stop")]
            expect(tried, "emergency_stop on an offline mower never reached the adapter")
    return True


def undeclared_commands(
    caps: MowerCapabilities, mower_map: MowerMap
) -> list[tuple[str, MowerCommand]]:
    """Commands a client must not offer for these capabilities, labelled by what is missing."""
    out: list[tuple[str, MowerCommand]] = []
    if not caps.manual_drive:
        out.append(("manual_drive", ManualDrive(linear=0.2, angular=0.0, ttl_ms=500)))
        out.append(("manual_drive: drive_calibrate", DriveCalibrate()))
    if caps.blade_height_range_mm is None:
        out.append(("blade_height_range_mm", SetBladeHeight(mm=50)))
    else:
        low, high = caps.blade_height_range_mm
        if high < 120:
            out.append(("blade height above the range", SetBladeHeight(mm=high + 1)))
        elif low > 10:
            out.append(("blade height below the range", SetBladeHeight(mm=low - 1)))
    if caps.max_speed_mps is None:
        out.append(("max_speed_mps", SetSpeed(mps=0.3)))
    if not caps.light:
        out.append(("light", SetLight(on=True)))
    if not caps.dock_keeps_job:
        out.append(("dock_keeps_job", ReturnToDock(end_job=False)))
    if not caps.firmware_update:
        out.append(("firmware_update", FirmwareUpdate(version="0.0.1")))
    if not caps.base_station_firmware_update:
        out.append(("base_station_firmware_update", BaseStationFirmwareUpdate(version="0.0.1")))
    if caps.drive_speed_setting is None:
        out.append(("drive_speed_setting", SetDriveSpeed(speed_mps=0.3)))
    maintenance: list[tuple[str, MowerCommand]] = [
        ("restart_mower", RestartMower()),
        ("sync_map", SyncMap()),
        ("relocate_dock", RelocateDock()),
        ("resync_rtk", ResyncRtk()),
        ("leave_dock", LeaveDock()),
    ]
    out.extend((f"maintenance: {a}", c) for a, c in maintenance if a not in caps.maintenance)
    declared = {spec.key for spec in caps.settings}
    spare = next((key for key in SETTING_KEYS if key not in declared), None)
    if spare is not None:
        out.append((f"settings: {spare}", SetSetting(key=spare, value=True)))
    out.append(("map areas", StartMow(area_ids=[UNKNOWN_AREA])))
    if mower_map.areas:
        area = mower_map.areas[0].id
        names = ("stripes", "diagonal", "checkerboard", "edge_only", "random_angle")
        for name in names:
            if name not in caps.native_patterns:
                command = StartMow(area_ids=[area], pattern={"name": name})
                out.append((f"native_patterns: {name}", command))
                break
    return out


async def check_undeclared_commands_refused(mower: MowerUnderTest) -> None:
    """ADR 0066 principle and ADR 0012: what the capabilities do not declare is refused by the
    controller with a readable reason, before the adapter is asked (no dead button reaches the
    vendor). The kit records the calls that hand a command to the adapter."""
    controller = await mower.start()
    caps = await controller.capabilities(refresh=True)
    mower_map = await controller.mower_map()
    offenders: list[str] = []
    for missing, command in undeclared_commands(caps, mower_map):
        before = len(mower.calls) if mower.calls is not None else 0
        result = await mower.send(command)
        if result.status is not CommandStatus.REJECTED or not result.reason:
            offenders.append(f"{missing}: {command.kind} was {describe(result)}")
        elif mower.calls is not None and len(mower.calls) != before:
            offenders.append(
                f"{missing}: {command.kind} reached the adapter {mower.calls[before:]}"
            )
    expect(not offenders, "undeclared commands not refused cleanly:\n" + "\n".join(offenders))


def declared_commands(caps: MowerCapabilities) -> list[tuple[str, MowerCommand]]:
    """Commands at rest that the capabilities offer: blade height and speed within their
    ranges, the light on and off."""
    out: list[tuple[str, MowerCommand]] = []
    if caps.blade_height_range_mm is not None:
        low, high = caps.blade_height_range_mm
        out.append(("blade_height_range_mm", SetBladeHeight(mm=max(10, min(120, high)))))
        if low != high:
            out.append(("blade_height_range_mm", SetBladeHeight(mm=max(10, min(120, low)))))
    if caps.max_speed_mps is not None:
        out.append(("max_speed_mps", SetSpeed(mps=min(2.0, caps.max_speed_mps))))
    if caps.light:
        out.append(("light", SetLight(on=True)))
        out.append(("light", SetLight(on=False)))
    return out


async def check_declared_commands_accepted(mower: MowerUnderTest) -> list[str]:
    """ADR 0066 principle ("no dead buttons"): a command the capabilities offer is taken by the
    adapter (executed or accepted). Returns what was checked."""
    controller = await mower.start()
    caps = await controller.capabilities(refresh=True)
    checked: list[str] = []
    offenders: list[str] = []
    for declared, command in declared_commands(caps):
        result = await mower.send(command)
        checked.append(f"{command.kind} {_args(command)}")
        if result.status not in OK:
            offenders.append(f"{declared}: {command.kind} {_args(command)} was {describe(result)}")
    expect(not offenders, "declared commands refused:\n" + "\n".join(offenders))
    return checked


async def check_start_plans(mower: MowerUnderTest) -> int:
    """ADR 0066 amendment: every declared start plan starts the mower (sent as
    ``StartMow.plan_ref``), and the connection validates the reference, so one it does not
    offer is refused. Returns the number of plans checked."""
    controller = await mower.start()
    caps = await controller.capabilities(refresh=True)
    if not caps.start_plans:
        return 0
    for plan in caps.start_plans:
        result = await mower.send(StartMow(plan_ref=plan.ref))
        expect(result.status in OK, f"start plan {plan.ref!r} ({plan.kind}): {describe(result)}")
        await _settle(mower)
        result = await mower.send(ReturnToDock(end_job=True))
        expect(result.status in OK, f"return_to_dock after plan {plan.ref!r}: {describe(result)}")
        await _settle(mower)
    result = await mower.send(StartMow(plan_ref=UNKNOWN_PLAN))
    expect(
        result.status is CommandStatus.REJECTED,
        f"an unknown start plan was {describe(result)}, not refused",
    )
    return len(caps.start_plans)


async def check_keep_job_on_return(mower: MowerUnderTest) -> bool:
    """ADR 0066 amendment: a mower that declares ``dock_keeps_job`` takes ``ReturnToDock`` with
    ``end_job: false`` during a job. Returns False when it is not declared."""
    controller = await mower.start()
    caps = await controller.capabilities(refresh=True)
    if not caps.dock_keeps_job:
        return False
    start = start_command(caps, await controller.mower_map())
    expect(start is not None, "no start a client could send")
    assert start is not None  # noqa: S101 - narrowed for the type checker
    result = await mower.send(start)
    expect(result.status in OK, f"start: {describe(result)}")
    await _settle(mower)
    result = await mower.send(ReturnToDock(end_job=False))
    expect(result.status in OK, f"return_to_dock keeping the job: {describe(result)}")
    return True


async def check_native_pattern_start(mower: MowerUnderTest) -> str | None:
    """ADR 0012 "Native jobs first": a pattern listed in ``native_patterns`` starts on a map
    area. Returns the pattern checked, None without areas or patterns."""
    controller = await mower.start()
    caps = await controller.capabilities(refresh=True)
    mower_map = await controller.mower_map()
    if not mower_map.areas or not caps.native_patterns:
        return None
    pattern = sorted(caps.native_patterns)[0]
    command = StartMow(area_ids=[mower_map.areas[0].id], pattern={"name": pattern})
    result = await mower.send(command)
    expect(result.status in OK, f"start with native pattern {pattern!r}: {describe(result)}")
    await _settle(mower)
    await mower.send(ReturnToDock(end_job=True))
    return pattern


def sample_value(spec: SettingSpec, current: SettingValue | None) -> SettingValue:
    """A valid value of ``spec`` that differs from ``current`` where the type allows."""
    match spec.type:
        case SettingType.BOOL:
            return not bool(current)
        case SettingType.INT:
            assert spec.min is not None and spec.max is not None  # noqa: S101 - validated
            step = spec.step or 1
            top = spec.min + ((spec.max - spec.min) // step) * step
            return top if current == spec.min else spec.min
        case SettingType.ENUM:
            return next((o for o in spec.options if o != current), spec.options[0])
        case SettingType.TIME_WINDOW:
            return TimeWindow(start="22:00", end="06:00") if current is None else None
    raise AssertionError(f"unknown setting type {spec.type}")


async def _reported(mower: MowerUnderTest, key: str, value: SettingValue, wait_s: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while True:
        state = await mower.connection.get_state()
        if key in state.settings and state.settings[key] == value:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.05)


async def check_settings_round_trip(mower: MowerUnderTest) -> list[str]:
    """ADR 0046: every declared setting takes a valid value through ``SetSetting``; an
    ``executed`` outcome means "the mower's answer shows the new value", so the state then
    reports it (the kit waits up to 5 s for the pushed state). Returns the keys checked."""
    controller = await mower.start()
    caps = await controller.capabilities(refresh=True)
    offenders: list[str] = []
    for spec in caps.settings:
        state = await mower.connection.get_state()
        value = sample_value(spec, state.settings.get(spec.key))
        result = await mower.send(SetSetting(key=spec.key, value=value))
        if result.status not in OK:
            offenders.append(f"{spec.key} = {value!r}: {describe(result)}")
        elif result.status is CommandStatus.EXECUTED and not await _reported(
            mower, spec.key, value, 5.0
        ):
            offenders.append(f"{spec.key} = {value!r}: executed, but the state does not show it")
    expect(not offenders, "settings that do not round-trip:\n" + "\n".join(offenders))
    return [spec.key for spec in caps.settings]


async def check_manual_drive(mower: MowerUnderTest) -> str:
    """ADR 0012 manual driving: without ``manual_drive`` the connection refuses a drive session
    with ``CommandRejected``; with it, a session opens (or is refused now with
    ``CommandRejected``), takes a zero frame, and ``stop`` and ``close`` are idempotent
    (``DriveSession``: "Adapters must make stop and close idempotent"). Returns what happened."""
    controller = await mower.start()
    caps = await controller.capabilities(refresh=True)
    connection = mower.connection
    timeout = mower.subject.command_timeout_s
    if not caps.manual_drive:
        try:
            session = await asyncio.wait_for(connection.start_drive_session(), timeout)
        except CommandRejected:
            return "refused without manual_drive"
        except Exception as exc:
            raise AssertionError(
                f"start_drive_session raised {type(exc).__name__}, not CommandRejected"
            ) from exc
        with suppress(Exception):
            await session.close()
        raise AssertionError("a drive session opened although manual_drive is not declared")
    try:
        session = await asyncio.wait_for(connection.start_drive_session(), timeout)
    except CommandRejected as exc:
        return f"refused in the fake mower's state: {exc}"
    try:
        expect(session.is_open, "a new drive session is not open")
        await asyncio.wait_for(session.update(0.0, 0.0, False), timeout)
        await asyncio.wait_for(session.stop(), timeout)
        await asyncio.wait_for(session.stop(), timeout)
    finally:
        await asyncio.wait_for(session.close(), timeout)
    await asyncio.wait_for(session.close(), timeout)
    expect(not session.is_open, "the drive session is still open after close()")
    return "opened, zero frame, stopped and closed"


# --- The error lexicon ------------------------------------------------------------------------


def package_lexicons(package: InstalledPackage) -> list[ErrorLexiconProvider]:
    """The package's error lexicons, loaded like the Device-Gateway loads them."""
    registry = ErrorLexiconRegistry(package.source(ERROR_LEXICONS))
    registry.load_entry_points()
    return [p for v in registry.vendors() if (p := registry.provider(vendor=v)) is not None]


def check_lexicon(package: InstalledPackage) -> None:
    """ADR 0067: the lexicon is a valid contract (round trip through JSON), English is there
    for every entry, every entry spells its code the way the lexicon looks it up, and the
    lexicon is found for the package's mowers: by each cloud provider id it registers
    (``ErrorLexicon.cloud_providers``, empty meaning the vendor key) and by each connection kind
    it registers."""
    registry = ErrorLexiconRegistry(package.source(ERROR_LEXICONS))
    registry.load_entry_points()
    providers = package_lexicons(package)
    expect(providers, f"{package.name} ships no loadable error lexicon")
    for provider in providers:
        lexicon = provider.lexicon
        again = type(lexicon).model_validate_json(lexicon.model_dump_json())
        expect(again == lexicon, f"lexicon {lexicon.vendor} does not survive JSON")
        for entry in lexicon.entries:
            expect(entry.texts["en"].title.strip(), f"{lexicon.vendor} {entry.code}: no title")
            expect(
                provider.entry(entry.code) is entry,
                f"{lexicon.vendor} {entry.code}: the code does not find its own entry",
            )
    for entry_point in package.entry_points(CLOUD_PROVIDERS):
        expect(
            registry.provider(cloud_provider=entry_point.name) is not None,
            f"no lexicon of {package.name} serves its cloud provider {entry_point.name!r} "
            "(ErrorLexicon.cloud_providers, or a vendor key equal to the provider id)",
        )
    for entry_point in package.entry_points(CONNECTIONS):
        expect(
            registry.provider(connection=entry_point.name) is not None,
            f"no lexicon of {package.name} lists its connection kind {entry_point.name!r} "
            "(ErrorLexicon.connection_kinds)",
        )


def check_lexicon_covers(package: InstalledPackage, codes: Iterable[str]) -> int:
    """ADR 0067: the lexicon explains every code the adapter can report (the provider names
    them, usually its SDK's code table). Returns how many codes were checked."""
    providers = package_lexicons(package)
    wanted = sorted({str(code) for code in codes})
    unknown = [c for c in wanted if not any(p.entry(c) is not None for p in providers)]
    expect(not unknown, f"codes the lexicon does not explain: {unknown[:50]}")
    return len(wanted)


def locale_coverage(package: InstalledPackage) -> Mapping[str, Mapping[str, int]]:
    """Entries per locale of each lexicon (informational: only English is required)."""
    out: dict[str, dict[str, int]] = {}
    for provider in package_lexicons(package):
        counts: dict[str, int] = {}
        for entry in provider.lexicon.entries:
            for locale in entry.texts:
                counts[locale] = counts.get(locale, 0) + 1
        out[provider.vendor] = counts
    return out

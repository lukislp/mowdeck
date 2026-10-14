"""Option schemas of the connection kinds: what each adapter needs, described as data.

The schemas drive two things: Fleet's validation of ``connection_options`` before a mower is
stored (:func:`option_problems`), and ``GET /api/v1/fleet/connection-kinds``, from which the web
app generates its connection forms. A new adapter kind therefore needs a schema here and no UI
change (the app falls back to the English ``title`` and ``description`` for options it has no
translation for). The option semantics follow ``libs/mowdeck-control-mammotion/README.md``.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

OptionType = Literal["string", "integer", "number", "boolean"]


@dataclass(frozen=True, slots=True)
class OptionSpec:
    """One option of a connection kind."""

    key: str
    type: OptionType
    title: str
    description: str = ""
    required: bool = False
    """The option must be present (and a non-empty string for strings)."""
    secret: bool = False
    """Write-only: the API answers ``***`` and keeps the stored value when ``***`` comes back."""
    group: str | None = None
    """Options of the same group are alternatives: exactly one of them is needed."""
    advanced: bool = False
    """Rarely needed; the app shows it under "advanced"."""
    pattern: str | None = None
    format_hint: str | None = None
    """Readable form of ``pattern`` for messages, e.g. ``a MAC address like AA:BB:...``."""
    minimum: float | None = None
    maximum: float | None = None
    default: str | int | float | bool | None = None
    examples: tuple[str, ...] = ()
    managed: bool = False
    """Set by Fleet (from the linked account's device list); shown, never edited by clients."""


@dataclass(frozen=True, slots=True)
class KindSchema:
    """The options of one connection kind."""

    kind: str
    title: str
    description: str
    options: tuple[OptionSpec, ...] = ()
    extra_options: bool = False
    """Keys outside ``options`` are accepted and kept (the simulator's ``SimulationConfig``)."""
    groups: Mapping[str, str] = field(default_factory=dict)
    """Group key to its English title."""
    linked_account: bool = False
    """Mowers of this kind come from a linked vendor account (``linked_account_id`` and
    ``device_name`` at registration, ADR 0034) instead of credentials in the options."""
    supports_links: frozenset[str] = frozenset()
    """The additional link kinds a mower of this kind may carry next to its connection (ADR
    0038, e.g. ``ble_bridge`` next to the cloud). The options of a link are validated with the
    schema of the link kind itself (``SCHEMAS[link_kind]``). Fleet decides eligibility from this
    registry entry, never from the kind's name."""

    @property
    def managed_keys(self) -> frozenset[str]:
        """Keys Fleet sets itself (``managed`` options)."""
        return frozenset(o.key for o in self.options if o.managed)

    @property
    def secret_keys(self) -> frozenset[str]:
        """Keys whose values are secrets (``secret`` options)."""
        return frozenset(o.key for o in self.options if o.secret)


MAC_PATTERN: Final = r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$"
CREDENTIAL_REF_PATTERN: Final = r"^(env|file):\S+$"

_TIMEOUTS: Final = (
    OptionSpec(
        "map_sync_timeout_s",
        "number",
        "Map sync timeout (s)",
        "How long to wait for the map after connecting.",
        advanced=True,
        minimum=1,
        maximum=3600,
        default=300,
    ),
    OptionSpec(
        "confirm_timeout_s",
        "number",
        "Confirmation timeout (s)",
        "How long a command waits for the mower to confirm it.",
        advanced=True,
        minimum=1,
        maximum=600,
        default=30,
    ),
)

SCHEMAS: Final[Mapping[str, KindSchema]] = {
    "simulated": KindSchema(
        kind="simulated",
        title="Simulated",
        description=(
            "A simulated mower in the Device-Gateway with a demo garden; it needs no options. "
            "Simulator settings such as time_scale are kept as they are; stuck_after_seconds "
            "(development only) makes the mower get stuck after that many seconds of mowing; "
            "night takes dark photos unless the light is on (AI assist light handling)."
        ),
        extra_options=True,
    ),
    "cloud": KindSchema(
        kind="cloud",
        title="Mammotion cloud",
        description=(
            "Connects through the Mammotion cloud (MQTT) with a linked Mammotion account: link "
            "the account once, then pick its mowers. Use a second account the mowers are shared "
            "with: logging in with the main account logs the phone app out."
        ),
        linked_account=True,
        supports_links=frozenset({"ble_bridge"}),
        options=(
            OptionSpec(
                "device_name",
                "string",
                "Device name",
                "The mower's name in the linked account, set from its device list.",
                managed=True,
            ),
            OptionSpec(
                "iot_id",
                "string",
                "IoT id",
                "The mower's IoT id in the Mammotion cloud, set from the device list.",
                managed=True,
            ),
            OptionSpec(
                "session_cache_path",
                "string",
                "Session cache file",
                "File for the cloud session tokens on a private, writable volume; avoids a "
                "password login on every reconnect.",
                advanced=True,
                examples=("/var/lib/device-gateway/mammotion-session.json",),
            ),
            OptionSpec(
                "refresh_seconds",
                "number",
                "State refresh (s)",
                "How often the mower is asked for a fresh status report; the Mammotion cloud "
                "sends reports only on request.",
                advanced=True,
                minimum=10,
                maximum=300,
                default=20,
            ),
            OptionSpec(
                "refresh_seconds_active",
                "number",
                "State refresh while mowing (s)",
                "How often the mower is asked for a fresh status report while it mows, returns "
                "or is paused away from the dock; more requests than the idle refresh, so more 4G "
                "traffic while it works.",
                advanced=True,
                minimum=2,
                maximum=60,
                default=5,
            ),
            OptionSpec(
                "report_stream_seconds",
                "number",
                "Live reports while mowing (s)",
                "Length of the continuous report window kept open while the mower works (renewed "
                "every half window); about one report per second, which keeps the live position "
                "smooth. 0 turns it off and uses the plain refresh. Only while mowing, so no "
                "extra 4G traffic while docked.",
                advanced=True,
                minimum=0,
                maximum=300,
                default=60,
            ),
            *_TIMEOUTS,
        ),
    ),
    "ble_bridge": KindSchema(
        kind="ble_bridge",
        title="BLE bridge (ESPHome)",
        description=(
            "Connects over Bluetooth through an ESPHome Bluetooth proxy near the mower; manual "
            "driving works only over this link."
        ),
        groups={"credential": "Encryption key"},
        options=(
            OptionSpec(
                "host",
                "string",
                "Proxy host",
                "Host name or IP address of the ESPHome proxy.",
                required=True,
                examples=("mowdeck-bridge.local",),
            ),
            OptionSpec(
                "port",
                "integer",
                "Port",
                "ESPHome API port.",
                minimum=1,
                maximum=65535,
                default=6053,
            ),
            OptionSpec(
                "noise_psk",
                "string",
                "Encryption key",
                "The API encryption key from the ESPHome YAML (base64, 32 bytes); never returned.",
                secret=True,
                group="credential",
            ),
            OptionSpec(
                "credential_ref",
                "string",
                "Key reference",
                "env:NAME or file:/run/secrets/name holding the encryption key.",
                group="credential",
                pattern=CREDENTIAL_REF_PATTERN,
                format_hint="env:NAME or file:/path",
            ),
            OptionSpec(
                "ble_address",
                "string",
                "BLE address",
                "The mower's Bluetooth MAC address.",
                required=True,
                pattern=MAC_PATTERN,
                format_hint="a MAC address like AA:BB:CC:DD:EE:FF",
                examples=("AA:BB:CC:DD:EE:FF",),
            ),
            OptionSpec(
                "device_name",
                "string",
                "Device name",
                "Advertised name; read from the advertisement when empty.",
                advanced=True,
            ),
            OptionSpec(
                "allow_manual_blades",
                "boolean",
                "Blades while driving manually",
                "Off keeps the blades off while the mower is driven manually.",
                default=False,
            ),
            OptionSpec(
                "manual_blade_height_mm",
                "integer",
                "Blade height for manual driving (mm)",
                "",
                advanced=True,
                minimum=10,
                maximum=120,
                default=60,
            ),
            OptionSpec(
                "scan_timeout_s",
                "number",
                "Scan timeout (s)",
                "How long to look for the mower's advertisement.",
                advanced=True,
                minimum=1,
                maximum=600,
                default=30,
            ),
            *_TIMEOUTS,
        ),
    ),
}
"""Every connection kind Fleet knows; ``MOWDECK_ALLOWED_CONNECTION_KINDS`` selects the offered."""


def links_supported(connection_kind: str) -> frozenset[str]:
    """The link kinds a mower with ``connection_kind`` may carry (empty for a kind without a
    schema or without links)."""
    schema = SCHEMAS.get(connection_kind)
    return schema.supports_links if schema is not None else frozenset()


LEGACY_CLOUD_SCHEMA: Final = KindSchema(
    kind="cloud",
    title="Mammotion cloud",
    description=(
        "Connects through the Mammotion cloud (MQTT) with a Mammotion account. Use a second "
        "account the mower is shared with: logging in with the main account logs the phone "
        "app out."
    ),
    groups={"device": "Mower", "credential": "Credential"},
    supports_links=frozenset({"ble_bridge"}),
    options=(
        OptionSpec(
            "account",
            "string",
            "Account (e-mail)",
            "The Mammotion account the mower is shared with.",
            required=True,
            examples=("second-account@example.com",),
        ),
        OptionSpec(
            "password",
            "string",
            "Password",
            "Plain password; accepted with a warning, never returned. Prefer a credential "
            "reference.",
            secret=True,
            group="credential",
        ),
        OptionSpec(
            "credential_ref",
            "string",
            "Credential reference",
            "env:NAME or file:/run/secrets/name, resolved in the Device-Gateway's environment.",
            group="credential",
            pattern=CREDENTIAL_REF_PATTERN,
            format_hint="env:NAME or file:/path",
            examples=("env:MAMMOTION_PASSWORD",),
        ),
        OptionSpec(
            "device_name",
            "string",
            "Device name",
            "The mower's name as the Mammotion app shows it, e.g. Luba-VP...",
            group="device",
            examples=("Luba-VP12345678",),
        ),
        OptionSpec(
            "iot_id",
            "string",
            "IoT id",
            "The mower's IoT id instead of its name.",
            group="device",
        ),
        OptionSpec(
            "session_cache_path",
            "string",
            "Session cache file",
            "File for the cloud session tokens on a private, writable volume; avoids a "
            "password login on every reconnect.",
            advanced=True,
            examples=("/var/lib/device-gateway/mammotion-session.json",),
        ),
        OptionSpec(
            "refresh_seconds",
            "number",
            "State refresh (s)",
            "How often the mower is asked for a fresh status report; the Mammotion cloud "
            "sends reports only on request.",
            advanced=True,
            minimum=10,
            maximum=300,
            default=20,
        ),
        OptionSpec(
            "refresh_seconds_active",
            "number",
            "State refresh while mowing (s)",
            "How often the mower is asked for a fresh status report while it mows, returns "
            "or is paused away from the dock; more requests than the idle refresh, so more 4G "
            "traffic while it works.",
            advanced=True,
            minimum=2,
            maximum=60,
            default=5,
        ),
        OptionSpec(
            "report_stream_seconds",
            "number",
            "Live reports while mowing (s)",
            "Length of the continuous report window kept open while the mower works (renewed "
            "every half window); about one report per second, which keeps the live position "
            "smooth. 0 turns it off and uses the plain refresh. Only while mowing, so no "
            "extra 4G traffic while docked.",
            advanced=True,
            minimum=0,
            maximum=300,
            default=60,
        ),
        *_TIMEOUTS,
    ),
)
"""Cloud mowers registered before linked accounts (ADR 0034) with their own ``account`` and a
``credential_ref`` or stored ``password``: their options are still validated against this schema
until they are deleted. New cloud mowers come from a linked account."""


@dataclass(frozen=True, slots=True)
class OptionProblem:
    """One violated rule: the option (the first of a group) and a message for the caller."""

    key: str
    message: str
    code: str


def _present(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def _type_ok(spec: OptionSpec, value: Any) -> bool:
    match spec.type:
        case "string":
            return isinstance(value, str)
        case "boolean":
            return isinstance(value, bool)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return isinstance(value, int | float) and not isinstance(value, bool)


def _value_problem(spec: OptionSpec, value: Any) -> OptionProblem | None:
    if spec.type in ("integer", "number"):
        low = spec.minimum if spec.minimum is not None else float("-inf")
        high = spec.maximum if spec.maximum is not None else float("inf")
        if not _type_ok(spec, value) or not low <= value <= high:
            what = "an integer" if spec.type == "integer" else "a number"
            bounds = f" between {_num(spec.minimum)} and {_num(spec.maximum)}"
            message = f"{spec.key} must be {what}{bounds if spec.minimum is not None else ''}"
            return OptionProblem(spec.key, message, "range")
        return None
    if not _type_ok(spec, value):
        return OptionProblem(spec.key, f"{spec.key} must be a {spec.type}", "type")
    if spec.pattern and isinstance(value, str) and not re.match(spec.pattern, value):
        hint = spec.format_hint or f"match {spec.pattern}"
        return OptionProblem(spec.key, f"{spec.key} must be {hint}", "pattern")
    return None


def _num(value: float | None) -> str:
    return str(int(value)) if value is not None and value == int(value) else str(value)


def option_problems(schema: KindSchema, options: Mapping[str, Any]) -> list[OptionProblem]:
    """Every rule of ``schema`` that ``options`` violates (empty when they are acceptable).

    Required options must be present (non-empty strings); of each group exactly one alternative
    is needed (more than one is accepted, the adapter prefers the first); present values must
    have the declared type, range and pattern. Keys outside the schema are not checked here.
    """
    problems: list[OptionProblem] = []
    groups: dict[str, list[OptionSpec]] = {}
    for spec in schema.options:
        if spec.group:
            groups.setdefault(spec.group, []).append(spec)
    reported: set[str] = set()
    for spec in schema.options:
        value = options.get(spec.key)
        if spec.group and spec.group not in reported:
            members = groups[spec.group]
            if not any(_present(options.get(m.key)) for m in members):
                names = " or ".join(sorted(m.key for m in members))
                problems.append(OptionProblem(spec.key, f"one of {names} is required", "one_of"))
            reported.add(spec.group)
        if spec.required and not _present(value):
            problems.append(OptionProblem(spec.key, f"{spec.key} is required", "required"))
            continue
        if value is None or (spec.type == "string" and value == ""):
            continue
        found = _value_problem(spec, value)
        if found:
            problems.append(found)
    return problems

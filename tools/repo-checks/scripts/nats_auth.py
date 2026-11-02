"""The NATS users of MowDeck and the subjects each one may use (ADR 0074).

Usage (from the repository root)::

    uv run poe nats-auth          # writes infra/compose/nats/auth.conf and the chart's copy
    uv run poe nats-auth-check    # exit 1 when a committed file is stale
    python tools/repo-checks/scripts/nats_auth.py --summary   # the map as a Markdown table

:data:`USERS` below is the one reviewable subject map: one user per service (the user name is
the Compose service name) plus ``streams``, the one-shot job that creates and updates the
JetStream streams. Each entry lists what the service's code does on the bus:

* ``publish``: subjects it publishes on, or sends core NATS requests to;
* ``serve``: core NATS subjects it answers (request/reply; replies go to the requester's inbox
  through ``allow_responses``);
* ``consume``: stream filters it reads through JetStream push consumers.

From that the generator derives the server permissions, least privilege by construction:

* publish: the listed subjects, ``$JS.API.STREAM.INFO.<stream>`` of every stream (the services
  wait for the streams), ``$JS.API.CONSUMER.CREATE.<stream>.*.<filter>`` for each consumed
  filter (the subject based API: the server checks the filter in the API subject against the
  request), ``$JS.API.CONSUMER.INFO.<stream>.*`` and ``$JS.ACK.<stream>.>`` of the consumed
  streams; ``streams`` alone may create and update the streams;
* subscribe: the served subjects and the user's own inbox ``_INBOX.<user>.>`` (replies and
  the delivery subjects of its consumers), never another user's inbox.

The repository check ``tools/repo-checks/tests/test_nats_auth.py`` keeps the map in step with
the code: every subject a service's code subscribes to, publishes on or requests (found with
``ast`` like the architecture diagrams, plus the event types it emits) must be allowed, and
every allowed subject must be used by the code.
"""

import argparse
import ast
import importlib.util
import re
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Literal

from mowdeck_contracts import v1 as contracts
from mowdeck_contracts.v1 import subjects as contract_subjects

AUTH_CONF = Path("infra/compose/nats/auth.conf")
"""The generated server configuration (included by the NATS server of Compose)."""
CHART_AUTH_CONF = Path("infra/helm/mowdeck/files/nats-auth.conf")
"""Identical copy for the Helm chart (a chart cannot read files outside its directory)."""

STREAMS_USER = "streams"
"""The user of the one-shot job ``nats-streams`` (``python -m mowdeck_service_kit.bus.streams``)."""

Kind = Literal["publish", "serve", "consume"]


@dataclass(frozen=True)
class Access:
    """What one NATS user does on the bus (see the module docstring)."""

    publish: tuple[str, ...] = ()
    serve: tuple[str, ...] = ()
    consume: tuple[str, ...] = ()
    manage_streams: bool = False

    def entries(self) -> Iterable[tuple[Kind, str]]:
        yield from (("publish", s) for s in self.publish)
        yield from (("serve", s) for s in self.serve)
        yield from (("consume", s) for s in self.consume)


# Every service that asks Entitlements about plans (mowdeck_service_kit.entitlements).
_PLANS: tuple[str, ...] = ("entitlements.check",)
_PLAN_CHANGES: tuple[str, ...] = ("event.entitlements.plan.changed",)
# Every service that drops cached authorization decisions on grant changes (mowdeck_authz).
_GRANTS: tuple[str, ...] = ("event.identity.grants.changed",)

USERS: dict[str, Access] = {
    "billing": Access(
        publish=("event.billing.>", "entitlements.plan.set"),
        consume=("event.identity.account.created", *_GRANTS),
    ),
    "camera-relay": Access(
        publish=(
            "camera.mower.*.relay.*",
            "gateway.mower.*.camera.release",
            "gateway.mower.*.camera.session",
        ),
        serve=("relay.camera.attach", "relay.camera.detach", "relay.camera.renew"),
        consume=("telemetry.mower.*",),
    ),
    "device-gateway": Access(
        publish=(
            "command.result.mower.*",
            "device.mower.*.capabilities",
            "device.mower.*.map",
            "device.mower.*.path",
            "event.mower.connected",
            "event.mower.disconnected",
            "fleet.mowers.credentials",
            "fleet.mowers.list",
            "telemetry.mower.*",
        ),
        serve=(
            "gateway.cloud.discover",
            "gateway.cloud.providers",
            "gateway.errors.entries",
            "gateway.errors.explain",
            "gateway.errors.lexicons",
            "gateway.mower.*.bridge.scan",
            "gateway.mower.*.camera.release",
            "gateway.mower.*.camera.session",
            "gateway.mower.*.firmware.check",
            "gateway.mower.*.path.preview",
            "gateway.mower.*.photo",
            "gateway.mower.*.tracking",
            "presence.accounts",
        ),
        consume=("command.mower.*", "event.mower.>"),
    ),
    "entitlements": Access(
        publish=("event.entitlements.>",),
        serve=("entitlements.check", "entitlements.plan.set"),
        consume=("event.identity.account.created", *_GRANTS),
    ),
    "fleet": Access(
        publish=(
            "command.mower.*",
            *_PLANS,
            "event.fleet.>",
            "event.mower.registered",
            "event.mower.removed",
            "event.mower.updated",
            "gateway.cloud.discover",
            "gateway.cloud.providers",
            "gateway.mower.*.bridge.scan",
            "gateway.mower.*.camera.session",
            "gateway.mower.*.firmware.check",
            "gateway.mower.*.path.preview",
            "gateway.mower.*.photo",
            "relay.camera.attach",
            "relay.camera.detach",
            "relay.camera.renew",
        ),
        serve=(
            "fleet.commands.issue",
            "fleet.drive.start",
            "fleet.drive.stop",
            "fleet.mowers.credentials",
            "fleet.mowers.list",
            "fleet.targets.resolve",
        ),
        consume=(
            "camera.mower.*.relay.*",
            "command.result.mower.*",
            "event.identity.account.created",
            *_GRANTS,
            *_PLAN_CHANGES,
        ),
    ),
    "identity": Access(
        publish=(*_PLANS, "event.identity.>"),
        consume=(*_GRANTS, *_PLAN_CHANGES),
    ),
    "notifications": Access(
        publish=(
            *_PLANS,
            "event.notifications.>",
            "fleet.mowers.list",
            "gateway.errors.explain",
            "gateway.mower.*.photo",
        ),
        consume=(
            "command.mower.*",
            "device.mower.>",
            "event.identity.>",
            "event.mower.>",
            "event.telemetry.>",
            "event.tracking.>",
            "event.vision.>",
            *_PLAN_CHANGES,
            "telemetry.mower.*",
        ),
    ),
    "planner": Access(
        publish=(*_PLANS, "tracks.positions.query"),
        consume=("device.mower.>", *_GRANTS, *_PLAN_CHANGES),
    ),
    "realtime": Access(
        publish=(
            "command.mower.*",
            *_PLANS,
            "fleet.drive.start",
            "fleet.drive.stop",
            "presence.accounts",
        ),
        consume=(
            "command.result.mower.*",
            "device.mower.*.capabilities",
            *_GRANTS,
            *_PLAN_CHANGES,
            "event.notifications.>",
            "event.vision.proposal.>",
            "telemetry.mower.*",
        ),
    ),
    "scheduler": Access(
        publish=(*_PLANS, "event.scheduler.>", "fleet.commands.issue", "fleet.targets.resolve"),
        consume=("device.mower.>", *_GRANTS, *_PLAN_CHANGES, "telemetry.mower.*"),
    ),
    "telemetry": Access(
        publish=(
            "event.telemetry.>",
            "event.tracking.>",
            "fleet.mowers.list",
            "gateway.errors.entries",
            "gateway.errors.explain",
            "gateway.errors.lexicons",
            "gateway.mower.*.photo",
            "gateway.mower.*.tracking",
        ),
        serve=("tracks.positions.query",),
        consume=(
            "command.result.mower.*",
            "device.mower.>",
            "event.mower.>",
            "event.notifications.>",
            "telemetry.mower.*",
        ),
    ),
    "vision": Access(
        publish=(
            "command.mower.*",
            *_PLANS,
            "event.vision.>",
            "fleet.commands.issue",
            "fleet.drive.start",
            "fleet.drive.stop",
            "gateway.mower.*.photo",
        ),
        consume=(
            "command.mower.*",
            "command.result.mower.*",
            "device.mower.>",
            "event.identity.>",
            *_PLAN_CHANGES,
            "telemetry.mower.*",
        ),
    ),
    STREAMS_USER: Access(manage_streams=True),
}

NOT_IN_THE_CODE: Mapping[tuple[str, Kind, str], str] = {}
"""Allowed subjects the code scan cannot see, with the reason (none at the moment)."""


# --- Configuration ----------------------------------------------------------------------------


def password_variable(user: str) -> str:
    """Environment variable of a user's password: ``NATS_PASSWORD_<USER>``."""
    return "NATS_PASSWORD_" + user.upper().replace("-", "_")


def stream_of(subject: str) -> str:
    """Name of the stream that stores ``subject`` (a subject or filter), or ``""``."""
    concrete = subject.replace("*", "x").replace(">", "x")
    spec = contract_subjects.stream_for_subject(concrete)
    return spec.name if spec else ""


def stream_names() -> list[str]:
    return [spec.name for spec in contract_subjects.STREAMS]


def check_users(users: Mapping[str, Access]) -> list[str]:
    """Problems of the map itself: unknown streams, served stream subjects, bad names."""
    problems: list[str] = []
    for user, access in users.items():
        if not re.fullmatch(r"[a-z][a-z0-9-]*", user):
            problems.append(f"{user}: not a valid user name")
        for subject in access.consume:
            if not stream_of(subject):
                problems.append(f"{user}: consumes {subject}, which no stream stores")
        for subject in access.serve:
            if stream_of(subject):
                problems.append(f"{user}: serves {subject}, which a stream captures")
        for kind, subject in access.entries():
            if not re.fullmatch(r"[a-z_][a-z0-9_*>-]*(\.[a-z0-9_*>-]+)*", subject):
                problems.append(f"{user}: {kind} {subject!r} is not a subject")
        if len(set(access.entries())) != len(list(access.entries())):
            problems.append(f"{user}: an entry is listed twice")
    return problems


def permissions(user: str, access: Access) -> tuple[list[str], list[str]]:
    """The ``(publish allow, subscribe allow)`` lists of one user."""
    publish = sorted(access.publish)
    for name in stream_names():
        publish.append(f"$JS.API.STREAM.INFO.{name}")
        if access.manage_streams:
            publish += [f"$JS.API.STREAM.CREATE.{name}", f"$JS.API.STREAM.UPDATE.{name}"]
    consumed: dict[str, list[str]] = defaultdict(list)
    for subject in access.consume:
        consumed[stream_of(subject)].append(subject)
    for name in sorted(consumed):
        publish += [f"$JS.API.CONSUMER.CREATE.{name}.*.{f}" for f in sorted(consumed[name])]
        publish += [f"$JS.API.CONSUMER.INFO.{name}.*", f"$JS.ACK.{name}.>"]
    subscribe = [f"_INBOX.{user}.>", *sorted(access.serve)]
    return publish, subscribe


def render(users: Mapping[str, Access] = USERS) -> str:
    """The NATS server configuration (an ``authorization`` block) of the map."""
    problems = check_users(users)
    if problems:
        raise ValueError("; ".join(problems))
    lines = [
        "# GENERATED by tools/repo-checks/scripts/nats_auth.py (poe nats-auth); do not edit.",
        "# The NATS users of MowDeck and the subjects each may use (ADR 0074). Every password is",
        "# the environment variable NATS_PASSWORD_<USER> of the NATS server: letters, digits, '-'",
        "# and '_', starting with a letter (infra/compose/nats-passwords.sh generates them). A",
        "# client without credentials is refused.",
        "authorization {",
        "  users = [",
    ]
    for user in sorted(users):
        access = users[user]
        publish, subscribe = permissions(user, access)
        lines += [
            "    {",
            f'      user: "{user}"',
            f"      password: ${password_variable(user)}",
            "      permissions: {",
            "        publish: {",
            "          allow: [",
            *(f'            "{subject}"' for subject in publish),
            "          ]",
            "        }",
            "        subscribe: {",
            "          allow: [",
            *(f'            "{subject}"' for subject in subscribe),
            "          ]",
            "        }",
        ]
        if access.serve:
            # Answer a request once, to the requester's inbox, within two minutes.
            lines.append("        allow_responses: true")
        lines += ["      }", "    }"]
    lines += ["  ]", "}", ""]
    return "\n".join(lines)


def summary(users: Mapping[str, Access] = USERS) -> str:
    """The map as a Markdown table (for the ADR and the pull request)."""
    rows = [
        "| User | Publishes and requests | Answers (core NATS) | Consumes (JetStream) |",
        "| --- | --- | --- | --- |",
    ]
    for user in sorted(users):
        access = users[user]
        cells = [
            "<br>".join(f"`{s}`" for s in sorted(subjects)) or "-"
            for subjects in (access.publish, access.serve, access.consume)
        ]
        if access.manage_streams:
            cells[0] = "creates and updates the streams " + ", ".join(stream_names())
        rows.append(f"| `{user}` | " + " | ".join(cells) + " |")
    return "\n".join(rows) + "\n"


# --- Code scan --------------------------------------------------------------------------------

_SUBSCRIBE = {"subscribe", "_subscribe"}
_REQUEST = {"request", "_request"}
_PUBLISH = {"publish", "_publish", "emit", "connection_event", "_connection_event"}
_CORE = {"fleet", "gateway", "entitlements", "tracks", "relay", "presence"}
"""First tokens of the core NATS subjects (request/reply, and ``presence.accounts``)."""
_SUBJECT = re.compile(r"[a-z_][a-z0-9_*>-]*(\.[a-z0-9_*>-]+)+")

LIB_ENTRY_POINTS: Mapping[str, tuple[tuple[Kind, str], ...]] = {
    # mowdeck_service_kit.entitlements: the client asks entitlements.check and drops its cache
    # on plan changes (EntitlementsClient._ask and .subscribe).
    "client_from_settings": (
        ("publish", "ENTITLEMENTS_CHECK"),
        ("consume", "PLAN_CHANGED_SUBJECT"),
    ),
    # mowdeck_authz.events: cache invalidation on grant changes.
    "subscribe_cache_invalidation": (("consume", "GRANTS_CHANGED_SUBJECT"),),
}
"""Library functions that use the bus on the caller's behalf, by name: (kind, constant)."""


def _arch(root: Path) -> ModuleType:
    """The architecture diagram generator, whose constant evaluation the scan reuses."""
    path = root / "tools" / "repo-checks" / "scripts" / "arch_diagrams.py"
    spec = importlib.util.spec_from_file_location("arch_diagrams_for_nats_auth", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        msg = f"cannot load {path}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event_types() -> set[str]:
    """Every event type constant of the contracts (``mower.registered``, ...)."""
    subject_values = {v for v in vars(contract_subjects).values() if isinstance(v, str)}
    found: set[str] = set()
    for name, value in vars(contracts).items():
        if (
            name.isupper()
            and isinstance(value, str)
            and re.fullmatch(r"[a-z_]+(\.[a-z_]+)+", value)
            and value not in subject_values
        ):
            found.add(value)
    return found


def _call_name(node: ast.Call) -> tuple[str, str]:
    """``(method or function name, receiver name)`` of a call."""
    func = node.func
    if isinstance(func, ast.Attribute):
        receiver = func.value.func if isinstance(func.value, ast.Call) else func.value
        name = (
            receiver.attr
            if isinstance(receiver, ast.Attribute)
            else receiver.id
            if isinstance(receiver, ast.Name)
            else ""
        )
        return func.attr, name
    if isinstance(func, ast.Name):
        return func.id, ""
    return "", ""


def scan(root: Path) -> dict[str, set[tuple[Kind, str]]]:
    """What the code of every service does on the bus: ``{service: {(kind, subject)}}``."""
    arch = _arch(root)
    scope = arch.build_scope(root)
    helpers: dict[str, set[tuple[str, str]]] = arch._helper_usages(root, scope)
    types = event_types()

    def resolve(node: ast.expr | None) -> str | None:
        if node is None:
            return None
        value = arch._evaluate(node, scope.constants, scope.functions)
        return str(arch._normal(value)) if isinstance(value, str) else None

    def event_type(node: ast.expr | None) -> str | None:
        """A constant (never a call) that names an event type."""
        if not isinstance(node, ast.Name | ast.Attribute | ast.Constant):
            return None
        value = resolve(node)
        return value if value in types else None

    def constant(name: str) -> str:
        value = scope.constants.get(name)
        if not isinstance(value, str):
            msg = f"constant {name} not found in libs/*/src"
            raise KeyError(msg)
        return value

    found: dict[str, set[tuple[Kind, str]]] = defaultdict(set)
    for service_dir in sorted((root / "services").iterdir()):
        src = service_dir / "src"
        if not src.is_dir():
            continue
        uses = found[service_dir.name]
        for path in arch._walk_py(src):
            if path.name == "module.py":  # the descriptor lists event types, it sends nothing
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):  # an envelope as a dict: {"type": EVENT, ...}
                    for key, value in zip(node.keys, node.values, strict=True):
                        typed = event_type(value)
                        if isinstance(key, ast.Constant) and key.value == "type" and typed:
                            uses.add(("publish", f"event.{typed}"))
                if not isinstance(node, ast.Call):
                    continue
                name, receiver = _call_name(node)
                for _kind, subject in helpers.get(name, ()):
                    if not subject.startswith("event.*"):  # publish_event: the type comes below
                        uses.add(("publish", subject))
                for kind, const in LIB_ENTRY_POINTS.get(name, ()):
                    uses.add((kind, constant(const)))
                for keyword in node.keywords:  # EventEnvelope(type=EVENT, ...)
                    typed = event_type(keyword.value)
                    if keyword.arg == "type" and typed:
                        uses.add(("publish", f"event.{typed}"))
                if name not in _SUBSCRIBE | _REQUEST | _PUBLISH or not node.args:
                    continue
                if name in _PUBLISH and "bus" not in receiver:
                    typed = event_type(node.args[0])
                    if typed:  # events.publish(EVENT, ...), work.emit(EVENT, ...)
                        uses.add(("publish", f"event.{typed}"))
                        continue
                first = resolve(node.args[0])
                if first is None or not _SUBJECT.fullmatch(first):  # e.g. httpx's request("GET")
                    continue
                if name in _SUBSCRIBE:
                    uses.add(("consume" if stream_of(first) else "serve", first))
                elif name in _REQUEST or stream_of(first) or first.split(".")[0] in _CORE:
                    uses.add(("publish", first))
    return dict(found)


def covers(pattern: str, subject: str) -> bool:
    """True when every subject matching ``subject`` (itself possibly a wildcard) matches
    ``pattern``."""
    pattern_tokens, subject_tokens = pattern.split("."), subject.split(".")
    for index, part in enumerate(pattern_tokens):
        if part == ">":
            return len(subject_tokens) > index
        if index >= len(subject_tokens) or subject_tokens[index] == ">":
            return False
        if part not in ("*", subject_tokens[index]):
            return False
    return len(pattern_tokens) == len(subject_tokens)


def drift(
    users: Mapping[str, Access], used: Mapping[str, set[tuple[Kind, str]]]
) -> tuple[list[str], list[str]]:
    """``(missing, unused)``: code uses the map does not allow, and allowed entries no code
    uses (apart from :data:`NOT_IN_THE_CODE`)."""
    missing: list[str] = []
    unused: list[str] = []
    for service, uses in sorted(used.items()):
        access = users.get(service)
        if access is None:
            if uses:
                missing.append(f"{service}: uses the bus but has no NATS user")
            continue
        allowed = list(access.entries())
        for kind, subject in sorted(uses):
            if not any(k == kind and covers(p, subject) for k, p in allowed):
                missing.append(f"{service}: {kind} {subject}")
        for kind, pattern in allowed:
            if (service, kind, pattern) in NOT_IN_THE_CODE:
                continue
            if not any(k == kind and covers(pattern, s) for k, s in uses):
                unused.append(f"{service}: {kind} {pattern}")
    return missing, unused


# --- Command line -----------------------------------------------------------------------------


def stale(root: Path) -> list[Path]:
    """The committed files that differ from a fresh generation."""
    text = render()
    return [p for p in (AUTH_CONF, CHART_AUTH_CONF) if _read(root / p) != text]


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail when a file is stale")
    mode.add_argument("--summary", action="store_true", help="print the map as Markdown")
    args = parser.parse_args(argv)
    root: Path = args.root
    if args.summary:
        sys.stdout.write(summary())
        return 0
    if args.check:
        outdated = stale(root)
        for path in outdated:
            print(f"{path} is stale: run `uv run poe nats-auth`", file=sys.stderr)
        return 1 if outdated else 0
    text = render()
    for path in (AUTH_CONF, CHART_AUTH_CONF):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

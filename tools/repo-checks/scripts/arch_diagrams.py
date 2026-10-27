"""Generate the architecture diagrams (Mermaid) from the code, so they never go stale.

Usage (from the repository root)::

    uv run poe arch-diagrams          # writes docs/architecture/generated.md
    uv run poe arch-diagrams-check    # exit 1 when the committed page is stale

Everything on the page is derived from a source of truth, nothing is drawn by hand:

* Services, profiles, Traefik routes, databases, platform dependencies and external systems:
  ``infra/compose/docker-compose.yml`` (parsed as YAML; external systems from the environment
  variable names of each service).
* NATS subjects and JetStream streams: the constants and helper functions of
  ``libs/*/src`` and ``services/*/src`` (read with ``ast``, never imported) and every
  ``subscribe``, ``publish`` and ``request`` call on a bus object in ``services/*/src``.
* HTTP API surface: the OpenAPI documents in ``apps/web/openapi`` (``tools``, ``paths``).
* Data ownership: the database of each service (compose) and its Alembic revisions.

The output is deterministic (sorted, no timestamps), so a diff shows real architecture changes
only. The repository checks compare the committed page with a fresh generation, like the
OpenAPI export.
"""

import argparse
import ast
import json
import os
import re
import sys
from collections import ChainMap, defaultdict
from collections.abc import Iterable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PAGE = Path("docs/architecture/generated.md")
"""Default output page, relative to the repository root."""

COMPOSE = Path("infra/compose/docker-compose.yml")
OPENAPI_DIR = Path("apps/web/openapi")
SUBJECTS_FILE = Path("libs/mowdeck-contracts/src/mowdeck_contracts/v1/subjects.py")

_SKIP_DIRS = {"tests", "migrations", "__pycache__", ".venv", "node_modules", "build", "dist"}
_BUS_RECEIVERS = {"bus", "_bus", "events", "_events"}
_BUS_METHODS = {"subscribe", "publish", "request"}

# External systems, recognised by the environment variable names a service is configured with.
# (regular expression on the variable name, node label). A variable that appears in a service
# means the service talks to that system when it is configured.
EXTERNAL_RULES: tuple[tuple[str, str], ...] = (
    (r"GITHUB_CLIENT", "GitHub OAuth"),
    (r"APPLE_(SERVICES|TEAM|KEY|PRIVATE)", "Sign in with Apple"),
    (r"APNS_", "Apple Push Notification service"),
    (r"VAPID_", "Web Push services of the browsers"),
    (r"STRIPE_", "Stripe"),
    (r"WEATHER_PROVIDER", "Weather provider (default Open-Meteo)"),
    (r"MAMMOTION", "Mammotion cloud and mower links"),
    (r"^AGORA_", "Agora (live camera)"),
    (r"VISION_.*PROVIDER", "Vision model provider"),
)

CLIENTS_LABEL = "Browser, mobile apps,<br/>Home Assistant integration"


# --- Small helpers ----------------------------------------------------------------------------


def _node_id(name: str) -> str:
    return re.sub(r"\W", "_", name)


def _label(text: str) -> str:
    """A Mermaid-safe quoted label (keeps ``<br/>``)."""
    return '"' + text.replace('"', "#quot;") + '"'


def _walk_py(base: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield Path(dirpath) / filename


def _subject_matches(pattern: str, subject: str) -> bool:
    pattern_tokens = pattern.split(".")
    subject_tokens = subject.split(".")
    for index, part in enumerate(pattern_tokens):
        if part == ">":
            return len(subject_tokens) > index
        if index >= len(subject_tokens):
            return False
        if part not in ("*", subject_tokens[index]):
            return False
    return len(pattern_tokens) == len(subject_tokens)


# --- Compose ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """One Traefik router of a service."""

    name: str
    prefixes: tuple[str, ...]
    exact: tuple[str, ...]


@dataclass(frozen=True)
class ComposeService:
    """What the Compose file says about one service."""

    name: str
    profiles: tuple[str, ...]
    own_image: bool
    one_shot: bool
    routes: tuple[Route, ...]
    database: str | None
    nats: bool
    openfga: bool
    jwks: bool
    http_peers: tuple[str, ...]
    external: tuple[str, ...]


def _env(service: Mapping[str, Any]) -> dict[str, str]:
    raw = service.get("environment") or {}
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for item in raw:
            key, _, value = str(item).partition("=")
            out[key] = value
        return out
    return {str(k): "" if v is None else str(v) for k, v in raw.items()}


def _labels(service: Mapping[str, Any]) -> dict[str, str]:
    raw = service.get("labels") or {}
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for item in raw:
            key, _, value = str(item).partition("=")
            out[key] = value
        return out
    return {str(k): str(v) for k, v in raw.items()}


def _routes(labels: Mapping[str, str]) -> tuple[Route, ...]:
    routes: list[Route] = []
    for key, rule in sorted(labels.items()):
        match = re.fullmatch(r"traefik\.http\.routers\.([^.]+)\.rule", key)
        if not match:
            continue
        routes.append(
            Route(
                name=match.group(1),
                prefixes=tuple(re.findall(r"PathPrefix\(`([^`]+)`\)", rule)),
                exact=tuple(re.findall(r"(?<![A-Za-z])Path\(`([^`]+)`\)", rule)),
            )
        )
    return tuple(routes)


def _builds_from_repo(raw: Mapping[str, Any]) -> bool:
    image = str(raw.get("image", ""))
    return "MOWDECK_IMAGE_REGISTRY" in image or image.startswith("mowdeck-local/")


def load_compose(root: Path) -> dict[str, ComposeService]:
    """Parse the Compose file into :class:`ComposeService` records, keyed by service name."""
    document = yaml.safe_load((root / COMPOSE).read_text(encoding="utf-8"))
    raw_services: Mapping[str, Mapping[str, Any]] = document["services"]
    names = set(raw_services)
    result: dict[str, ComposeService] = {}
    for name, raw in sorted(raw_services.items()):
        env = _env(raw)
        database: str | None = None
        for value in env.values():
            match = re.match(r"postgresql://[^@]*@postgres:\d+/(\w+)", value)
            if match:
                database = match.group(1)
        peers = sorted(
            {
                m
                for key, value in env.items()
                if not key.endswith("JWKS_URL")
                for m in re.findall(r"http://([a-z0-9-]+):\d+", value)
                if m in names and m != name and _builds_from_repo(raw_services[m])
            }
        )
        external = sorted(
            {label for key in env for pattern, label in EXTERNAL_RULES if re.search(pattern, key)}
        )
        result[name] = ComposeService(
            name=name,
            profiles=tuple(raw.get("profiles") or ()),
            own_image=_builds_from_repo(raw),
            one_shot=str(raw.get("restart", "")) == "no",
            routes=_routes(_labels(raw)),
            database=database,
            nats=any(k.endswith("NATS_URL") for k in env),
            openfga=any(k.endswith("OPENFGA_API_URL") for k in env),
            jwks=any(k.endswith("AUTH_JWKS_URL") for k in env),
            http_peers=tuple(peers),
            external=tuple(external),
        )
    return result


def _is_app(service: ComposeService) -> bool:
    """A long-running service built from this repository."""
    return service.own_image and not service.one_shot


def _profile_title(profile: str) -> str:
    return f"Profile {profile} (opt-in)"


def _routing_diagram(services: Mapping[str, ComposeService]) -> str:
    lines = [
        "flowchart LR",
        f"  clients([{_label(CLIENTS_LABEL)}])",
        '  traefik{{"Traefik<br/>gateway"}}',
    ]
    lines.append("  clients --> traefik")
    groups: dict[str, list[ComposeService]] = defaultdict(list)
    for svc in services.values():
        if svc.routes:
            groups[svc.profiles[0] if svc.profiles else ""].append(svc)
    edges: list[str] = []
    for profile in sorted(groups, key=lambda p: (p != "", p)):
        members = groups[profile]
        if profile:
            lines.append(f"  subgraph sg_{_node_id(profile)}[{_label(_profile_title(profile))}]")
        else:
            lines.append(f"  subgraph sg_always[{_label('Always on')}]")
        for svc in members:
            lines.append(f"    {_node_id(svc.name)}[{_label(svc.name)}]")
            prefixes = sorted({p for r in svc.routes for p in r.prefixes})
            exact = sorted({p for r in svc.routes for p in r.exact})
            text = "<br/>".join(f"{p}" for p in prefixes)
            if exact:
                text += f"<br/>+{len(exact)} exact path{'s' if len(exact) != 1 else ''}"
            style = "-.->" if profile else "-->"
            edges.append(f"  traefik {style}|{_label(text)}| {_node_id(svc.name)}")
        lines.append("  end")
    return "\n".join([*lines, *edges])


def _platform_signature(service: ComposeService) -> tuple[str, ...]:
    parts = []
    if service.database:
        parts.append("PostgreSQL")
    if service.nats:
        parts.append("NATS")
    if service.openfga:
        parts.append("OpenFGA")
    if service.jwks:
        parts.append("JWKS")
    return tuple(parts)


def _platform_diagram(services: Mapping[str, ComposeService]) -> str:
    groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for svc in services.values():
        if _is_app(svc) and svc.name != "web" and _platform_signature(svc):
            groups[_platform_signature(svc)].append(svc.name)
    lines = ["flowchart LR"]
    edges: list[str] = []
    for index, signature in enumerate(sorted(groups, key=lambda s: (len(s), s))):
        lines.append(f"  subgraph g{index}[{_label(' + '.join(signature))}]")
        lines.extend(f"    {_node_id(n)}[{_label(n)}]" for n in sorted(groups[signature]))
        lines.append("  end")
        for dep in signature:
            edges.append(f"  g{index} --> {_node_id(dep)}")
    lines.append('  PostgreSQL[("PostgreSQL<br/>one database per service")]')
    lines.append('  NATS{{"NATS JetStream<br/>events, commands, telemetry"}}')
    lines.append('  OpenFGA["OpenFGA<br/>authorization"]')
    lines.append('  JWKS(["identity<br/>/.well-known/jwks.json"])')
    return "\n".join([*lines, *edges])


def _external_diagram(services: Mapping[str, ComposeService]) -> str:
    lines = ["flowchart LR"]
    externals = sorted({e for s in services.values() for e in s.external})
    users = sorted({s.name for s in services.values() if s.external})
    lines.append("  subgraph mowdeck[MowDeck]")
    lines.extend(f"    {_node_id(n)}[{_label(n)}]" for n in users)
    lines.append("  end")
    lines.append("  subgraph outside[External systems]")
    lines.extend(f"    x{index}([{_label(e)}])" for index, e in enumerate(externals))
    lines.append("  end")
    for svc in sorted(services.values(), key=lambda s: s.name):
        lines.extend(f"  {_node_id(svc.name)} --> x{externals.index(e)}" for e in svc.external)
    return "\n".join(lines)


def _ownership_diagram(root: Path, services: Mapping[str, ComposeService]) -> str:
    lines = ["flowchart LR"]
    edges: list[str] = []
    dbs = sorted((s for s in services.values() if s.database and _is_app(s)), key=lambda s: s.name)
    lines.append("  subgraph apps[Services]")
    lines.extend(f"    {_node_id(s.name)}[{_label(s.name)}]" for s in dbs)
    lines.append("  end")
    lines.append("  subgraph pg[PostgreSQL]")
    for svc in dbs:
        count = migration_count(root, svc.name)
        text = f"{svc.database}" + (f"<br/>{count} migrations" if count else "")
        lines.append(f"    db_{_node_id(svc.name)}[({_label(text)})]")
        edges.append(f"  {_node_id(svc.name)} -->|owns| db_{_node_id(svc.name)}")
    lines.append("  end")
    return "\n".join([*lines, *edges])


def migration_count(root: Path, service: str) -> int:
    """Number of Alembic revision files of ``services/<service>`` (0 without Alembic)."""
    ini = root / "services" / service / "alembic.ini"
    if not ini.is_file():
        return 0
    match = re.search(r"^script_location\s*=\s*%\(here\)s/(\S+)", ini.read_text("utf-8"), re.M)
    if not match:
        return 0
    versions = ini.parent / match.group(1) / "versions"
    return (
        len([p for p in versions.glob("*.py") if p.name != "__init__.py"])
        if versions.is_dir()
        else 0
    )


# --- NATS -------------------------------------------------------------------------------------


@dataclass
class _Scope:
    """Module-level constants and helper functions found in the source."""

    constants: dict[str, str] = field(default_factory=dict)
    functions: dict[str, ast.FunctionDef] = field(default_factory=dict)


_MISSING = object()


def _evaluate(
    node: ast.expr, scope: Mapping[str, Any], funcs: Mapping[str, ast.FunctionDef]
) -> Any:
    """Evaluate a string expression of constants, f-strings and helper calls; None if unknown."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else _MISSING
    if isinstance(node, ast.Name):
        return scope.get(node.id, _MISSING)
    if isinstance(node, ast.Attribute):
        return scope.get(node.attr, _MISSING)
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            elif isinstance(value, ast.FormattedValue):
                inner = _evaluate(value.value, scope, funcs)
                if not isinstance(inner, str):
                    return _MISSING
                parts.append(inner)
        return "".join(parts)
    if isinstance(node, ast.Call):
        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else None
        )
        if name is not None and name in funcs:
            return _call(funcs[name], node, scope, funcs)
    return _MISSING


def _call(
    fn: ast.FunctionDef,
    call: ast.Call,
    scope: Mapping[str, Any],
    funcs: Mapping[str, ast.FunctionDef],
) -> Any:
    """Run a small string helper (``return f"..."``) with the call's arguments bound."""
    params = [a.arg for a in fn.args.args]
    local: dict[str, Any] = {}
    defaults = fn.args.defaults
    for param, default in zip(params[len(params) - len(defaults) :], defaults, strict=True):
        local[param] = (
            None
            if isinstance(default, ast.Constant) and default.value is None
            else (_evaluate(default, scope, funcs))
        )
    for param in params:
        local.setdefault(param, _unknown(param))
    for param, arg in zip(params, call.args, strict=False):
        local[param] = _arg_value(arg, param, scope, funcs)
    for keyword in call.keywords:
        if keyword.arg in local:
            local[keyword.arg] = _arg_value(keyword.value, keyword.arg, scope, funcs)
    chain: MutableMapping[str, Any] = ChainMap(local, dict(scope))
    for stmt in fn.body:
        if isinstance(stmt, ast.If):
            test = stmt.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and isinstance(test.ops[0], ast.Is)
                and chain.get(test.left.id, _MISSING) is None
                and isinstance(stmt.body[0], ast.Return)
                and stmt.body[0].value is not None
            ):
                return _evaluate(stmt.body[0].value, chain, funcs)
        elif isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
            _assign_partition(stmt, chain, local)
        elif isinstance(stmt, ast.Return) and stmt.value is not None:
            return _evaluate(stmt.value, chain, funcs)
    return _MISSING


def _arg_value(
    arg: ast.expr, param: str, scope: Mapping[str, Any], funcs: Mapping[str, ast.FunctionDef]
) -> Any:
    if isinstance(arg, ast.Constant) and arg.value is None:
        return None
    value = _evaluate(arg, scope, funcs)
    if isinstance(value, str):
        return value
    return _unknown(param)


def _unknown(param: str) -> Any:
    """A parameter without a known value: a wildcard for ids and kinds, else unresolvable."""
    return f"<{param}>" if param.endswith("id") or param == "kind" else _MISSING


def _assign_partition(
    stmt: ast.Assign, chain: MutableMapping[str, Any], local: dict[str, Any]
) -> None:
    """Support ``a, sep, b = value.partition(".")`` inside a helper."""
    call = stmt.value
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "partition"
        and isinstance(call.func.value, ast.Name)
        and isinstance(chain.get(call.func.value.id), str)
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(stmt.targets[0], ast.Tuple)
    ):
        return
    head, sep, tail = str(chain[call.func.value.id]).partition(str(call.args[0].value))
    for target, value in zip(stmt.targets[0].elts, (head, sep, tail), strict=True):
        if isinstance(target, ast.Name):
            local[target.id] = value


def build_scope(root: Path) -> _Scope:
    """Collect the module-level string constants and helper functions of libs and services."""
    pending: list[tuple[str, ast.expr]] = []
    scope = _Scope()
    bases = sorted([*(root / "libs").glob("*/src"), *(root / "services").glob("*/src")])
    for base in bases:
        for path in _walk_py(base):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.FunctionDef):
                    scope.functions.setdefault(node.name, node)
                elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                    target = node.targets[0]
                    if isinstance(target, ast.Name) and target.id.isupper():
                        pending.append((target.id, node.value))
                elif (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.value is not None
                    and node.target.id.isupper()
                ):
                    pending.append((node.target.id, node.value))
    for _ in range(6):
        changed = False
        for name, expr in pending:
            if name in scope.constants:
                continue
            value = _evaluate(expr, scope.constants, scope.functions)
            if isinstance(value, str):
                scope.constants[name] = value
                changed = True
        if not changed:
            break
    return scope


@dataclass(frozen=True)
class Stream:
    """A JetStream stream: its name and the subjects it captures."""

    name: str
    subjects: tuple[str, ...]

    def captures(self, subject: str) -> bool:
        return any(_subject_matches(p, subject) for p in self.subjects)


def load_streams(root: Path) -> list[Stream]:
    """The ``StreamSpec(...)`` definitions of the contracts, in declaration order."""
    tree = ast.parse((root / SUBJECTS_FILE).read_text(encoding="utf-8"))
    streams: list[Stream] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "StreamSpec":
            kwargs = {k.arg: k.value for k in node.keywords if k.arg}
            name, subjects = kwargs.get("name"), kwargs.get("subjects")
            if isinstance(name, ast.Constant) and isinstance(subjects, ast.Tuple):
                streams.append(
                    Stream(
                        str(name.value),
                        tuple(str(e.value) for e in subjects.elts if isinstance(e, ast.Constant)),
                    )
                )
    return sorted(streams, key=lambda s: s.name)


@dataclass(frozen=True)
class Usage:
    """One bus call site: ``kind`` is ``subscribe``, ``publish`` or ``request``."""

    service: str
    kind: str
    subject: str


def _receiver_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


DYNAMIC_EVENT = "event.* (type chosen at runtime)"
_CORE_DOMAINS = {"fleet", "gateway", "entitlements", "tracks", "relay"}


def _normal(subject: str) -> str:
    """Placeholders (``<id>``, ``<kind>``) become the NATS wildcard ``*``."""
    return re.sub(r"<[^>]*>", "*", subject)


def _bus_call(node: ast.AST) -> tuple[str, str, ast.expr] | None:
    """``(method, receiver, first argument)`` of a call on a bus object, else None."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _BUS_METHODS
        and _receiver_name(node.func.value) in _BUS_RECEIVERS
        and node.args
    ):
        return node.func.attr, _receiver_name(node.func.value), node.args[0]
    return None


def _helper_usages(root: Path, scope: _Scope) -> dict[str, set[tuple[str, str]]]:
    """Bus calls inside the helper functions of ``libs/*/src``, by function name.

    A service that calls ``publish_command(bus, envelope)`` publishes on ``command.mower.*``
    even though the subject is only built in the service kit.
    """
    helpers: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for base in sorted((root / "libs").glob("*/src")):
        for path in _walk_py(base):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in tree.body:
                if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                for node in ast.walk(fn):
                    call = _bus_call(node)
                    if call is None or call[0] == "subscribe":
                        continue
                    value = _evaluate(call[2], scope.constants, scope.functions)
                    if isinstance(value, str):
                        helpers[fn.name].add((call[0], _normal(value)))
                    elif "event" in fn.name:
                        helpers[fn.name].add((call[0], DYNAMIC_EVENT))
    return helpers


def scan_usages(root: Path, scope: _Scope, streams: Sequence[Stream]) -> tuple[list[Usage], int]:
    """Find every bus call of ``services/*/src``; returns the usages and the unresolved count."""
    usages: set[Usage] = set()
    unresolved = 0
    helpers = _helper_usages(root, scope)
    for service_dir in sorted((root / "services").iterdir()):
        src = service_dir / "src"
        if not src.is_dir():
            continue
        service = service_dir.name
        for path in _walk_py(src):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    callee = _receiver_name(node.func)
                    for kind, subject in sorted(helpers.get(callee, ())):
                        usages.add(Usage(service, kind, subject))
                call = _bus_call(node)
                if call is None:
                    continue
                method, receiver, first = call
                value = _evaluate(first, scope.constants, scope.functions)
                if not isinstance(value, str):
                    if method == "publish" and receiver in {"events", "_events"}:
                        usages.add(Usage(service, "publish", DYNAMIC_EVENT))
                    else:
                        unresolved += 1
                    continue
                value = _normal(value)
                # The domain-event publisher takes an event type ("identity.grants.changed"),
                # everything else a full subject.
                is_subject = stream_of(streams, value) is not None or (
                    value.split(".")[0] in _CORE_DOMAINS
                )
                if not is_subject and method == "publish" and "." in value:
                    value = f"event.{value}"
                usages.add(Usage(service, method, value))
    return sorted(usages, key=lambda u: (u.service, u.kind, u.subject)), unresolved


def stream_of(streams: Sequence[Stream], subject: str) -> Stream | None:
    """The stream that stores ``subject`` (a concrete subject or a filter), if any."""
    concrete = subject.replace("*", "x")
    for stream in streams:
        if stream.captures(concrete):
            return stream
    return None


def _edge_label(subjects: Iterable[str]) -> str:
    ordered = sorted(set(subjects))
    shown = ordered[:3]
    text = "<br/>".join(shown)
    if len(ordered) > len(shown):
        text += f"<br/>+{len(ordered) - len(shown)} more"
    return _label(text)


def _stream_diagrams(usages: Sequence[Usage], streams: Sequence[Stream]) -> dict[str, str]:
    diagrams: dict[str, str] = {}
    for stream in streams:
        pubs: dict[str, set[str]] = defaultdict(set)
        subs: dict[str, set[str]] = defaultdict(set)
        for usage in usages:
            if usage.subject == DYNAMIC_EVENT:
                if stream.name == "EVENTS":
                    pubs[usage.service].add(usage.subject)
                continue
            if stream_of(streams, usage.subject) != stream:
                continue
            (subs if usage.kind == "subscribe" else pubs)[usage.service].add(usage.subject)
        if not pubs and not subs:
            continue
        lines = ["flowchart LR", f"  s_{stream.name}{{{{{_label(stream.name)}}}}}"]
        edges = []
        for service in sorted(pubs):
            lines.append(f"  p_{_node_id(service)}[{_label(service)}]")
            edges.append(
                f"  p_{_node_id(service)} -->|{_edge_label(pubs[service])}| s_{stream.name}"
            )
        for service in sorted(subs):
            lines.append(f"  c_{_node_id(service)}[{_label(service)}]")
            edges.append(
                f"  s_{stream.name} -->|{_edge_label(subs[service])}| c_{_node_id(service)}"
            )
        diagrams[stream.name] = "\n".join([*lines, *edges])
    return diagrams


def _request_diagrams(usages: Sequence[Usage], streams: Sequence[Stream]) -> dict[str, str]:
    core = [
        u for u in usages if u.subject != DYNAMIC_EVENT and stream_of(streams, u.subject) is None
    ]
    responders: dict[str, set[str]] = defaultdict(set)
    for usage in core:
        if usage.kind == "subscribe":
            responders[usage.service].add(usage.subject)
    diagrams: dict[str, str] = {}
    for responder in sorted(responders):
        edges: dict[str, set[str]] = defaultdict(set)
        for usage in core:
            if usage.kind == "subscribe":
                continue
            concrete = usage.subject.replace("*", "x")
            if any(_subject_matches(p, concrete) for p in responders[responder]):
                edges[usage.service].add(usage.subject)
        if not edges:
            continue
        lines = ["flowchart LR", f"  r_{_node_id(responder)}[{_label(responder + ' (answers)')}]"]
        for requester in sorted(edges):
            lines.append(f"  q_{_node_id(requester)}[{_label(requester)}]")
            lines.append(
                f"  q_{_node_id(requester)} -->|{_edge_label(edges[requester])}|"
                f" r_{_node_id(responder)}"
            )
        diagrams[responder] = "\n".join(lines)
    return diagrams


# --- OpenAPI ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ApiGroup:
    """The operations of one OpenAPI tag."""

    tag: str
    operations: int
    paths: tuple[str, ...]


def load_api(root: Path) -> dict[str, list[ApiGroup]]:
    """OpenAPI tags per service, from the committed documents."""
    result: dict[str, list[ApiGroup]] = {}
    for path in sorted((root / OPENAPI_DIR).glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        counts: dict[str, int] = defaultdict(int)
        paths: dict[str, set[str]] = defaultdict(set)
        for route, item in document.get("paths", {}).items():
            for method, operation in item.items():
                if method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                for tag in operation.get("tags") or ["(untagged)"]:
                    counts[tag] += 1
                    paths[tag].add(route)
        result[path.stem] = [
            ApiGroup(tag, counts[tag], tuple(sorted(paths[tag]))) for tag in sorted(counts)
        ]
    return result


def _api_diagram(api: Mapping[str, Sequence[ApiGroup]]) -> str:
    """All services with their tags; the probe tag ``operations`` is left out."""
    lines = ["flowchart LR"]
    for service in sorted(api):
        lines.append(f"  subgraph {_node_id(service)}[{_label(service)}]")
        lines.append("    direction TB")
        for group in api[service]:
            if group.tag == PROBE_TAG:
                continue
            noun = "operation" if group.operations == 1 else "operations"
            text = f"{group.tag}<br/>{group.operations} {noun}"
            lines.append(f"    {_node_id(service)}_{_node_id(group.tag)}[{_label(text)}]")
        lines.append("  end")
    return "\n".join(lines)


PROBE_TAG = "operations"


# --- The page ---------------------------------------------------------------------------------


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(out)


def _fence(diagram: str) -> str:
    return f"```mermaid\n{diagram}\n```"


def build(root: Path) -> tuple[str, dict[str, str]]:
    """Return the page text and the individual diagrams (name to Mermaid source)."""
    services = load_compose(root)
    scope = build_scope(root)
    streams = load_streams(root)
    usages, unresolved = scan_usages(root, scope, streams)
    api = load_api(root)

    diagrams: dict[str, str] = {}
    out: list[str] = [
        "# Generated architecture diagrams",
        "",
        '!!! note "Generated, do not edit"',
        "    This page is written by `tools/repo-checks/scripts/arch_diagrams.py` from the code",
        "    and the Compose file. Run `uv run poe arch-diagrams` after changing a service, a",
        "    route, a NATS subject or the Compose stack; CI fails when the page is stale.",
        "    The hand-written diagrams of the",
        "    [architecture chapters](01-introduction-and-goals.md) and the ADRs explain the *why*;",
        "    this page shows what the code does *today*.",
        "",
        "## How this is generated",
        "",
        _table(
            ["Diagrams", "Source of truth"],
            [
                [
                    "Routing, platform, external systems, data ownership",
                    "`infra/compose/docker-compose.yml`: services, profiles, Traefik labels, "
                    "`MOWDECK_*` environment variables, Alembic revisions of `services/*`",
                ],
                [
                    "Messaging",
                    "`subscribe`, `publish` and `request` calls on the bus in `services/*/src`, "
                    "subject constants and helpers of `libs/*/src` (read with `ast`), stream "
                    "definitions in `mowdeck_contracts/v1/subjects.py`",
                ],
                [
                    "HTTP API",
                    "OpenAPI documents in `apps/web/openapi` (exported from the services)",
                ],
            ],
        ),
        "",
        "Not derivable from code and therefore not shown: the browser, mobile and Home Assistant "
        "clients are drawn as one node; subjects built at runtime are listed as such.",
        "",
        "## Services and routing",
        "",
        "Traefik is the only entry point. Dotted arrows are services that start only with a "
        "Compose profile.",
        "",
    ]
    diagrams["routing"] = _routing_diagram(services)
    out += [_fence(diagrams["routing"]), ""]

    rows = []
    for svc in sorted(services.values(), key=lambda s: s.name):
        if not svc.routes:
            continue
        rows.append(
            [
                f"`{svc.name}`",
                ", ".join(f"`{p}`" for p in sorted({p for r in svc.routes for p in r.prefixes})),
                ", ".join(f"`{p}`" for p in sorted({p for r in svc.routes for p in r.exact}))
                or "-",
                ", ".join(svc.profiles) or "always",
            ]
        )
    out += [_table(["Service", "Path prefixes", "Exact paths", "Profile"], rows), ""]

    one_shots = sorted(s.name for s in services.values() if s.one_shot)
    infra = sorted(s.name for s in services.values() if not s.own_image and not s.one_shot)
    out += [
        f"Infrastructure containers: {', '.join(f'`{n}`' for n in infra)}. "
        f"One-shot jobs (migrations, bootstrap): {', '.join(f'`{n}`' for n in one_shots)}.",
        "",
        "## Platform dependencies",
        "",
        "Services are grouped by what they depend on: a database, NATS, OpenFGA and the "
        "token keys of identity (JWKS).",
        "",
    ]
    diagrams["platform"] = _platform_diagram(services)
    out += [_fence(diagrams["platform"]), ""]
    peers = [
        [f"`{s.name}`", ", ".join(f"`{p}`" for p in s.http_peers)]
        for s in sorted(services.values(), key=lambda s: s.name)
        if s.http_peers and _is_app(s)
    ]
    out += [
        "Services that call other services over HTTP (configured by URL):",
        "",
        _table(["Service", "Calls"], peers),
        "",
        "## External systems",
        "",
        "Recognised from the environment variables each service is configured with.",
        "",
    ]
    diagrams["external"] = _external_diagram(services)
    out += [_fence(diagrams["external"]), ""]

    out += ["## Messaging", ""]
    out += [
        "Streams are the JetStream streams of the contracts; every other subject is core NATS "
        "request/reply. Subjects use `*` for the mower id.",
        "",
        "### JetStream streams",
        "",
    ]
    for name, diagram in _stream_diagrams(usages, streams).items():
        diagrams[f"stream-{name.lower()}"] = diagram
        spec = next(s for s in streams if s.name == name)
        out += [
            f"#### {name}",
            "",
            f"Captures `{'`, `'.join(spec.subjects)}`.",
            "",
            _fence(diagram),
            "",
        ]
    out += ["### Request and reply", ""]
    for responder, diagram in _request_diagrams(usages, streams).items():
        diagrams[f"requests-{responder}"] = diagram
        out += [f"#### Answered by {responder}", "", _fence(diagram), ""]

    by_subject: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for usage in usages:
        by_subject[usage.subject][usage.kind].add(usage.service)
    subject_rows = []
    for subject in sorted(by_subject):
        stream = stream_of(streams, subject)
        kinds = by_subject[subject]
        subject_rows.append(
            [
                f"`{subject}`",
                stream.name if stream else "core",
                ", ".join(sorted(kinds["publish"] | kinds["request"])) or "-",
                ", ".join(sorted(kinds["subscribe"])) or "-",
            ]
        )
    out += [
        "### All subjects",
        "",
        _table(
            ["Subject", "Stream", "Publishes or requests", "Subscribes or answers"], subject_rows
        ),
        "",
        f"{unresolved} bus calls build their subject at runtime from variables and are not listed. "
        "Shared libraries (for example the plan-change subscription of the service kit) are not "
        "attributed to a service.",
        "",
        "## HTTP API",
        "",
        "Operations per OpenAPI tag of each service, from the documents the web app's types "
        "are generated from.",
        "",
    ]
    diagrams["api"] = _api_diagram(api)
    out += [_fence(diagrams["api"]), ""]
    api_rows = [
        [f"`{service}`", f"`{g.tag}`", str(g.operations), str(len(g.paths))]
        for service in sorted(api)
        for g in api[service]
    ]
    out += [
        _table(["Service", "Tag", "Operations", "Paths"], api_rows),
        "",
        f"The tag `{PROBE_TAG}` holds the probe endpoints every service exposes.",
        "",
    ]

    out += ["## Data ownership", "", "Each service owns one database; nothing else reads it.", ""]
    diagrams["ownership"] = _ownership_diagram(root, services)
    out += [_fence(diagrams["ownership"]), ""]
    stateless = sorted(s.name for s in services.values() if _is_app(s) and not s.database)
    out += [f"Services without a database: {', '.join(f'`{n}`' for n in stateless)}.", ""]
    return "\n".join(out), diagrams


def stale(root: Path) -> bool:
    """True when the committed page differs from a fresh generation."""
    page = root / PAGE
    return not page.is_file() or page.read_text(encoding="utf-8") != build(root)[0]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--check", action="store_true", help="exit 1 when the page is stale")
    parser.add_argument("--emit-mermaid", type=Path, help="also write each diagram as <name>.mmd")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    root: Path = args.root
    text, diagrams = build(root)
    if args.emit_mermaid:
        args.emit_mermaid.mkdir(parents=True, exist_ok=True)
        for name, diagram in diagrams.items():
            (args.emit_mermaid / f"{name}.mmd").write_text(diagram + "\n", encoding="utf-8")
    page = root / PAGE
    if args.check:
        if not page.is_file() or page.read_text(encoding="utf-8") != text:
            print(f"{PAGE} is stale: run `uv run poe arch-diagrams`", file=sys.stderr)
            return 1
        return 0
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {PAGE} ({len(diagrams)} diagrams)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

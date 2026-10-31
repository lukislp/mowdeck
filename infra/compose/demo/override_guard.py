"""The allow-list of demo/demo.override.yml (docs/ops/demo.md, "Host specific settings").

demo/reset.sh pipes ``{"base": <config>, "override": <config>}`` into this script, where both
are ``docker compose config --format json`` of the demo stack, without and with the override.
The script compares them structurally and accepts only these changes:

* ``services.traefik.networks``: attach the gateway to networks outside the demo, with aliases
  or a fixed address (no alias may be the name of a service of the stack: the proxy would reach
  that name instead of the service);
* resource limits of an existing service: ``mem_limit``, ``mem_reservation``, ``memswap_limit``,
  ``cpus``, ``cpu_shares``, ``cpu_quota``, ``cpu_period``, ``cpuset``, ``pids_limit`` and
  ``deploy.resources`` (with the empty ``deploy.placement`` Compose adds along with it);
* ``services.<name>.logging``;
* new top-level ``networks`` that are ``external`` (created outside the demo, never by it).

Everything else is refused: other services or profiles, environment, labels (routers,
middlewares), command, image, ports, volumes, ``network_mode``, other ``deploy`` keys, networks
of any other service, changes of the demo's own network (or of the gateway on it). The script
names what it refuses by its path only (for example
``services.identity.environment.MOWDECK_CLIENT_IP_DEPTH``) and never prints a value: the merged
configuration holds the demo's secrets.

Standard library only (the reset host needs ``python3`` and nothing else). Exit status: 0
accepted, 1 refused, 2 unreadable input.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from typing import Any

Path = tuple[str, ...]

SERVICE_KEYS = frozenset(
    {
        "logging",
        "mem_limit",
        "mem_reservation",
        "memswap_limit",
        "cpus",
        "cpu_shares",
        "cpu_quota",
        "cpu_period",
        "cpuset",
        "pids_limit",
    }
)
"""Keys of an existing service the override may set or change, with everything below them."""

GATEWAY = "traefik"
"""The one service the override may attach to networks (the proxy's network, docs/ops/demo.md)."""

_MISSING = object()


def changes(base: Any, other: Any, path: Path = ()) -> Iterator[Path]:
    """The paths where ``other`` differs from ``base``: down to the leaves of nested mappings,
    a whole list or scalar where it differs, a key where it is added or removed."""
    base_map = isinstance(base, dict)
    other_map = isinstance(other, dict)
    if (
        (base_map or base is _MISSING)
        and (other_map or other is _MISSING)
        and (base_map or other_map)
    ):
        left = base if base_map else {}
        right = other if other_map else {}
        if not left and not right:
            # An empty mapping added or removed (``volumes: {data: {}}``) is a change too.
            if base is _MISSING or other is _MISSING:
                yield path
            return
        for key in sorted(set(left) | set(right), key=str):
            yield from changes(left.get(key, _MISSING), right.get(key, _MISSING), (*path, str(key)))
        return
    if base != other:
        yield path


def refusal(path: Path, base: dict[str, Any], override: dict[str, Any]) -> tuple[Path, str] | None:
    """Why the change at ``path`` is not allowed (with the path to name), or ``None``."""
    base_services = base.get("services") or {}
    if len(path) >= 2 and path[0] == "services":
        name = path[1]
        if name not in base_services or name not in (override.get("services") or {}):
            return path[:2], "adds or removes a service (or enables a profile)"
        if len(path) == 2:
            return path, "replaces a service"
        key = path[2]
        if key in SERVICE_KEYS:
            return None
        if key == "networks":
            return _network_refusal(path, base, override)
        if key == "deploy" and len(path) >= 4 and path[3] == "resources":
            return None
        if key == "deploy" and path[3:] == ("placement",) and _added_empty(path, base, override):
            # Compose renders ``placement: {}`` as soon as a service has ``deploy`` at all.
            return None
        return path, "changes more than networks, resource limits and logging"
    if len(path) >= 2 and path[0] == "networks":
        name = path[1]
        if name in (base.get("networks") or {}):
            return path, "changes a network of the demo"
        added = (override.get("networks") or {}).get(name)
        if not isinstance(added, dict) or added.get("external") is not True:
            return path[:2], "adds a network that is not external"
        return None
    return path, "changes more than services and networks"


def _at(document: dict[str, Any], path: Path) -> Any:
    value: Any = document
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return _MISSING
        value = value[key]
    return value


def _added_empty(path: Path, base: dict[str, Any], override: dict[str, Any]) -> bool:
    """The override adds ``path`` as an empty value that the base does not have."""
    return _at(base, path) is _MISSING and _at(override, path) in ({}, None)


def _network_refusal(
    path: Path, base: dict[str, Any], override: dict[str, Any]
) -> tuple[Path, str] | None:
    """Only the gateway joins networks, and only networks outside the demo, under aliases that
    are no service of the stack (an alias ``identity`` would answer for Identity)."""
    service = path[1]
    if service != GATEWAY:
        return path[:3], f"attaches a service other than the gateway ({GATEWAY}) to networks"
    if len(path) < 4:
        return path, "replaces the networks of the gateway"
    network = path[3]
    if network in (base.get("networks") or {}):
        # `- mowdeck` renders as null, `mowdeck: {}` as {}: the same attachment.
        if _at(base, path[:4]) in ({}, None) and _at(override, path[:4]) in ({}, None):
            return None
        return path[:4], "changes the gateway on a network of the demo"
    aliases = _at(override, (*path[:4], "aliases"))
    services = set(base.get("services") or {})
    if isinstance(aliases, list) and services.intersection(map(str, aliases)):
        return (*path[:4], "aliases"), "an alias is the name of a service of the demo"
    return None


def check(base: dict[str, Any], override: dict[str, Any]) -> list[str]:
    """Every refused change as ``<path>: <reason>`` (paths only, never values)."""
    found: list[str] = []
    for path in changes(base, override):
        refused = refusal(path, base, override)
        if refused is not None:
            line = f"{'.'.join(refused[0]) or '(root)'}: {refused[1]}"
            if line not in found:
                found.append(line)
    return found


def main() -> int:
    try:
        document = json.load(sys.stdin)
        base, override = document["base"], document["override"]
        if not isinstance(base, dict) or not isinstance(override, dict):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        print("override-guard: unreadable configuration", file=sys.stderr)
        return 2
    refused = check(base, override)
    for line in refused[:20]:
        print(f"override-guard: refused {line}", file=sys.stderr)
    if len(refused) > 20:
        print(f"override-guard: ... and {len(refused) - 20} more", file=sys.stderr)
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())

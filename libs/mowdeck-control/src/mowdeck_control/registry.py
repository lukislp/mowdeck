"""Registry of connection factories, one per :class:`~mowdeck_contracts.v1.ConnectionKind`.

Adapters register in code with :func:`register_connection` or, from another package, through
the Python entry point group ``mowdeck.connections``: the entry point name is the connection
kind and the object is the factory. Example from ``mowdeck-control-mammotion``::

    [project.entry-points."mowdeck.connections"]
    ble_bridge = "mowdeck_control_mammotion:create_ble_bridge_connection"
    cloud = "mowdeck_control_mammotion:create_cloud_connection"

This package registers the ``simulated`` kind the same way.
"""

import logging
from collections.abc import Callable, Iterable
from importlib.metadata import EntryPoint, entry_points

from mowdeck_contracts.v1 import ConnectionKind

from mowdeck_control.connection import ConnectionConfig, MowerConnection
from mowdeck_control.errors import UnknownConnectionKind

ENTRY_POINT_GROUP = "mowdeck.connections"

ConnectionFactory = Callable[[ConnectionConfig], MowerConnection]
EntryPointSource = Callable[[], Iterable[EntryPoint]]

_log = logging.getLogger("mowdeck_control.registry")


def _installed_entry_points() -> Iterable[EntryPoint]:
    return entry_points(group=ENTRY_POINT_GROUP)


class ConnectionRegistry:
    """Maps connection kinds to factories."""

    def __init__(self, entry_point_source: EntryPointSource = _installed_entry_points) -> None:
        self._factories: dict[ConnectionKind, ConnectionFactory] = {}
        self._entry_point_source = entry_point_source
        self._entry_points_loaded = False

    def register(
        self, kind: ConnectionKind | str, factory: ConnectionFactory, *, replace: bool = False
    ) -> None:
        """Register ``factory`` for ``kind``.

        Raises ``ValueError`` if another factory is registered for ``kind`` and ``replace`` is
        False. Registering the same factory again is a no-op.
        """
        key = ConnectionKind(kind)
        existing = self._factories.get(key)
        if existing is not None and existing is not factory and not replace:
            msg = f"a connection factory for '{key}' is already registered"
            raise ValueError(msg)
        self._factories[key] = factory

    def load_entry_points(self) -> list[ConnectionKind]:
        """Register every factory from the ``mowdeck.connections`` entry points.

        Entry points with an unknown kind or a failing import are logged and skipped; they never
        break the other adapters. Returns the kinds that were registered.
        """
        self._entry_points_loaded = True
        loaded: list[ConnectionKind] = []
        for entry_point in self._entry_point_source():
            try:
                kind = ConnectionKind(entry_point.name)
            except ValueError:
                _log.warning("ignoring entry point with unknown connection kind %s", entry_point)
                continue
            try:
                factory = entry_point.load()
            except Exception:
                _log.exception("cannot load connection entry point %s", entry_point)
                continue
            if not callable(factory):
                _log.warning("ignoring entry point %s: not callable", entry_point)
                continue
            if kind in self._factories and self._factories[kind] is not factory:
                _log.info("keeping the registered factory for %s over %s", kind, entry_point)
                continue
            self._factories[kind] = factory
            loaded.append(kind)
        return loaded

    def kinds(self) -> list[ConnectionKind]:
        """All registered kinds (entry points included), sorted."""
        self._ensure_entry_points()
        return sorted(self._factories)

    def factory(self, kind: ConnectionKind | str) -> ConnectionFactory:
        """Return the factory for ``kind`` or raise :class:`UnknownConnectionKind`."""
        try:
            key = ConnectionKind(kind)
        except ValueError:
            msg = f"unknown connection kind '{kind}'"
            raise UnknownConnectionKind(msg) from None
        if key not in self._factories:
            self._ensure_entry_points()
        try:
            return self._factories[key]
        except KeyError:
            msg = f"no connection adapter is installed for '{key}'"
            raise UnknownConnectionKind(msg) from None

    def create(self, kind: ConnectionKind | str, config: ConnectionConfig) -> MowerConnection:
        """Create a connection of ``kind`` for ``config``."""
        return self.factory(kind)(config)

    def _ensure_entry_points(self) -> None:
        if not self._entry_points_loaded:
            self.load_entry_points()


_default_registry = ConnectionRegistry()


def default_registry() -> ConnectionRegistry:
    """The process wide registry used by the module level functions."""
    return _default_registry


def register_connection(
    kind: ConnectionKind | str, factory: ConnectionFactory, *, replace: bool = False
) -> None:
    """Register ``factory`` for ``kind`` in the default registry."""
    _default_registry.register(kind, factory, replace=replace)


def create_connection(kind: ConnectionKind | str, config: ConnectionConfig) -> MowerConnection:
    """Create a connection from the default registry."""
    return _default_registry.create(kind, config)


def available_kinds() -> list[ConnectionKind]:
    """Kinds available in the default registry."""
    return _default_registry.kinds()

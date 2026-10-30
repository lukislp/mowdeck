"""What a provider package hands the conformance kit: :class:`ProviderUnderTest`.

The kit derives everything else from the installed distribution (its entry points, its
``pyproject.toml``) and from the objects those entry points register. A provider supplies only
what the kit cannot know: how its fake vendor client is reached.
"""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from mowdeck_contracts.v1 import DiscoveredDevice
from mowdeck_control import ConnectionConfig, MowerConnection

APP_LOCALES: Final = ("en", "de")
"""The languages of the web app (``apps/web/messages``); every provider text exists in each."""

ConnectionFactory = Callable[[ConnectionConfig], MowerConnection | Awaitable[MowerConnection]]
"""Builds one (not yet connected) connection on the package's fake vendor client."""


def _first_online_mower(device: DiscoveredDevice) -> bool:
    return device.is_mower and device.online is not False


@dataclass(frozen=True, kw_only=True)
class ProviderUnderTest:
    """A provider package and the few things the kit needs to drive it without a network.

    Give ``fake_credentials`` when the package registers a cloud provider
    (``mowdeck.cloud_providers``): the kit creates the plugin with ``ProviderContext(fake=True)``,
    discovers the fake account's devices and builds the connection of the device ``pick_device``
    selects, exactly as the Device-Gateway would. Give ``connection_factory`` instead (or in
    addition, it wins) when the connection is built another way, for example a package that only
    registers ``mowdeck.connections``.
    """

    distribution: str
    """The installed distribution, e.g. ``mowdeck-control-acme``."""
    fake_credentials: Mapping[str, str] | None = None
    """Credentials the provider's fake cloud accepts (by credential field key)."""
    connection_factory: ConnectionFactory | None = None
    """Builds a connection on the fake vendor client; overrides the cloud provider path."""
    pick_device: Callable[[DiscoveredDevice], bool] = _first_online_mower
    """Which discovered device the connection checks run on (default: the first online mower)."""
    mower_options: Mapping[str, Any] = field(default_factory=dict)
    """Extra connection options, e.g. short confirmation timeouts for the fake."""
    emitted_error_codes: Callable[[], Iterable[str]] | None = None
    """Every error code the adapter can put into ``MowerState.error_code`` (usually the vendor
    SDK's code table); the lexicon must explain each. None skips that check."""
    required_locales: tuple[str, ...] = APP_LOCALES
    """Locales every user-visible provider text must have."""
    command_timeout_s: float = 30.0
    """Upper bound for one command through the controller on the fake client."""
    emergency_stop_timeout_s: float = 5.0
    """How fast ``EmergencyStop`` must return while another command is still running."""
    known_violations: Mapping[str, str] = field(default_factory=dict)
    """Test name to reason: checks expected to fail until the reason is fixed (strict xfail, so
    a fixed violation shows up as an unexpected pass)."""

    @property
    def module(self) -> str:
        """The import name of the package (``mowdeck-control-acme`` gives
        ``mowdeck_control_acme``)."""
        return self.distribution.replace("-", "_")

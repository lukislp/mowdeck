"""The Mammotion cloud as a MowDeck cloud provider (ADR 0034, ADR 0066).

Registered under the entry point group ``mowdeck.cloud_providers`` as ``mammotion``
(:data:`SPEC`). The plugin owns one :class:`~mowdeck_control_mammotion.session.CloudSessionPool`
per gateway: the cloud connections of all mowers of one linked account borrow one logged-in
PyMammotion client from it, and a discovery reuses that session, so re-reading the account
does not log its connected mowers out.

With ``ProviderContext.fake`` the plugin answers from :mod:`mowdeck_control_mammotion.fake_cloud`
(accounts ``*@fake.mowdeck.test``, password ``fake-cloud-password``); that module is imported
only then, so a production gateway never loads it.

The provider is enabled by default (tested on a LUBA 2 AWD X) and also serves ``cloud``
registrations that name no provider: the cloud mowers registered before ADR 0066.
"""

from collections.abc import Callable, Mapping
from typing import Any, Final

from mowdeck_contracts.v1 import (
    ConnectionOptionSpec,
    CredentialField,
    CredentialFieldKind,
    ProviderDescriptor,
    ProviderLink,
)
from mowdeck_control import (
    CLOUD_PROVIDER_OPTION,
    CloudLoginRefused,
    CloudUnavailable,
    ConnectionConfig,
    MowerConnection,
    ProviderContext,
    ProviderDevices,
    ProviderSpec,
)

from mowdeck_control_mammotion import discovery
from mowdeck_control_mammotion.session import CloudSessionPool

PROVIDER: Final = "mammotion"

DESCRIPTOR: Final = ProviderDescriptor(
    provider=PROVIDER,
    name="Mammotion",
    summary=(
        "LUBA and Yuka mowers through the Mammotion cloud (MQTT): state, maps, native jobs, "
        "camera and manual driving."
    ),
    fields=[
        CredentialField(
            key="email",
            kind=CredentialFieldKind.EMAIL,
            label="E-mail address",
            identity=True,
            help="The Mammotion account the mowers are shared with.",
        ),
        CredentialField(
            key="password",
            kind=CredentialFieldKind.SECRET,
            label="Password",
            help="Stored encrypted and never shown again.",
            max_length=256,
        ),
    ],
    setup_steps=[
        "Create a second Mammotion account in the Mammotion app.",
        "Share your mowers with it (owner account: device settings, share).",
        "Link the second account here: a login with the main account logs the phone app out.",
    ],
    links=[ProviderLink(label="Mammotion app", url="https://mammotion.com/pages/app")],
    tested_on_hardware=True,
    mower_options=[
        ConnectionOptionSpec(
            key="session_cache_path",
            type="string",
            title="Session cache file",
            description="File for the cloud session tokens on a private, writable volume; "
            "avoids a password login on every reconnect.",
            advanced=True,
            examples=["/var/lib/device-gateway/mammotion-session.json"],
        ),
        ConnectionOptionSpec(
            key="refresh_seconds",
            type="number",
            title="State refresh (s)",
            description="How often the mower is asked for a fresh status report; the Mammotion "
            "cloud sends reports only on request.",
            advanced=True,
            minimum=10,
            maximum=300,
            default=20,
        ),
        ConnectionOptionSpec(
            key="refresh_seconds_active",
            type="number",
            title="State refresh while mowing (s)",
            description="How often the mower is asked for a fresh status report while it mows, "
            "returns or is paused away from the dock; more requests than the idle refresh, so "
            "more 4G traffic while it works.",
            advanced=True,
            minimum=2,
            maximum=60,
            default=5,
        ),
        ConnectionOptionSpec(
            key="report_stream_seconds",
            type="number",
            title="Live reports while mowing (s)",
            description="Length of the continuous report window kept open while the mower works "
            "(renewed every half window); about one report per second, which keeps the live "
            "position smooth. 0 turns it off and uses the plain refresh. Only while mowing, so "
            "no extra 4G traffic while docked.",
            advanced=True,
            minimum=0,
            maximum=300,
            default=60,
        ),
        ConnectionOptionSpec(
            key="all_cameras",
            type="boolean",
            title="All cameras",
            description="Ask the cloud for every camera of the mower (both front cameras of a "
            "LUBA 2 AWD X, the rear one of a Yuka) instead of the front left one only; off when "
            "a mower misbehaves with more than one stream.",
            advanced=True,
            default=True,
        ),
        ConnectionOptionSpec(
            key="cloud_drive",
            type="boolean",
            title="Manual driving over the cloud",
            description="Joystick, nudges, AI assist moves and spot runs over the Mammotion "
            "cloud when no Bluetooth bridge is connected (forwards and turns only, about 0.3 s "
            "delay). Needs the mower's owner account; off keeps driving to the bridge.",
            default=True,
        ),
        ConnectionOptionSpec(
            key="allow_manual_blades",
            type="boolean",
            title="Blades while driving manually",
            description="Off keeps the blades off while the mower is driven manually over the "
            "cloud.",
            default=False,
        ),
        ConnectionOptionSpec(
            key="manual_blade_height_mm",
            type="integer",
            title="Blade height for manual driving (mm)",
            advanced=True,
            minimum=20,
            maximum=100,
            default=60,
        ),
        ConnectionOptionSpec(
            key="manual_drive_speed_mps",
            type="number",
            title="Manual driving speed limit (m/s)",
            description="max_run_speed sent when a drive session opens; empty: the mower's own "
            "speed setting, at most 1.0.",
            advanced=True,
            minimum=0.1,
            maximum=1.0,
        ),
        ConnectionOptionSpec(
            key="map_sync_timeout_s",
            type="number",
            title="Map sync timeout (s)",
            description="How long to wait for the map after connecting.",
            advanced=True,
            minimum=1,
            maximum=3600,
            default=300,
        ),
        ConnectionOptionSpec(
            key="confirm_timeout_s",
            type="number",
            title="Confirmation timeout (s)",
            description="How long a command waits for the mower to confirm it.",
            advanced=True,
            minimum=1,
            maximum=600,
            default=30,
        ),
    ],
)


def connection_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """The options of :class:`~mowdeck_control_mammotion.options.CloudOptions`: the linked
    account's ``email`` becomes ``account`` (cloud mowers registered with their own login keep
    their ``account``), and the provider id is dropped."""
    out = {k: v for k, v in options.items() if k != CLOUD_PROVIDER_OPTION}
    email = out.pop("email", None)
    if email is not None and "account" not in out:
        out["account"] = email
    return out


DeviceLister = Callable[[Any], list[Any]]


class MammotionProvider:
    """:class:`~mowdeck_control.CloudProviderPlugin` on a shared session pool."""

    def __init__(
        self,
        pool: CloudSessionPool,
        factory: Callable[[ConnectionConfig], MowerConnection],
        lister: discovery.DeviceLister = discovery.registry_devices,
    ) -> None:
        self.pool = pool
        self._factory = factory
        self._lister = lister

    @property
    def descriptor(self) -> ProviderDescriptor:
        """:data:`DESCRIPTOR`."""
        return DESCRIPTOR

    async def discover(self, credentials: Mapping[str, str]) -> ProviderDevices:
        """Log in (or reuse the open session) and list the account's devices."""
        try:
            found = await discovery.discover_devices(
                self.pool,
                credentials.get("email", ""),
                credentials.get("password", ""),
                lister=self._lister,
            )
        except discovery.CloudLoginRefused as exc:
            raise CloudLoginRefused(str(exc)) from exc
        except discovery.CloudUnavailable as exc:
            raise CloudUnavailable(str(exc)) from exc
        return ProviderDevices(devices=found.devices, reused_session=found.reused_session)

    def create_connection(self, config: ConnectionConfig) -> MowerConnection:
        """A cloud connection (or the dual one with a Bluetooth bridge link) on the pool."""
        options = connection_options(config.options)
        return self._factory(
            ConnectionConfig(
                mower_id=config.mower_id, account_id=config.account_id, options=options
            )
        )

    async def close(self) -> None:
        """Stop every shared session."""
        await self.pool.close()


def create_provider(context: ProviderContext) -> MammotionProvider:
    """The plugin for ``context``: the real cloud (credential caches below
    ``context.cache_dir``) or the fake one."""
    if context.fake:
        from mowdeck_control_mammotion.fake_cloud import (
            fake_cloud_connection_factory,
            fake_devices,
            fake_pool,
        )

        pool = fake_pool()
        return MammotionProvider(pool, fake_cloud_connection_factory(pool), fake_devices)
    from mowdeck_control_mammotion.cloud import cloud_connection_factory
    from mowdeck_control_mammotion.port import login_cloud_client

    pool = CloudSessionPool(login_cloud_client, cache_dir=context.cache_dir)
    return MammotionProvider(pool, cloud_connection_factory(pool))


SPEC: Final = ProviderSpec(
    provider=PROVIDER, create=create_provider, enabled_by_default=True, unqualified_cloud=True
)
"""The entry point object (``mowdeck.cloud_providers``: ``mammotion``)."""

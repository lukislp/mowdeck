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
    CapabilityProfile,
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
    summary={
        "en": "LUBA and Yuka mowers through the Mammotion cloud (MQTT): state, maps, native jobs, "
        "camera and manual driving.",
        "de": "LUBA- und Yuka-Mäher über die Mammotion-Cloud (MQTT): Zustand, Karten, eigene "
        "Mähaufträge, Kamera und manuelles Fahren.",
    },
    fields=[
        CredentialField(
            key="email",
            kind=CredentialFieldKind.EMAIL,
            label={"en": "E-mail address", "de": "E-Mail-Adresse"},
            identity=True,
            help={
                "en": "The Mammotion account the mowers are shared with.",
                "de": "Das Mammotion-Konto, mit dem die Mäher geteilt sind.",
            },
        ),
        CredentialField(
            key="password",
            kind=CredentialFieldKind.SECRET,
            label={"en": "Password", "de": "Passwort"},
            max_length=256,
        ),
    ],
    setup_steps=[
        {
            "en": "Create a second Mammotion account in the Mammotion app.",
            "de": "Lege in der Mammotion-App ein zweites Mammotion-Konto an.",
        },
        {
            "en": "Share your mowers with it (owner account: device settings, share).",
            "de": "Teile deine Mäher mit diesem Konto (im Besitzerkonto: Geräteeinstellungen, "
            "Teilen).",
        },
        {
            "en": "Link the second account here: a login with the main account logs the phone "
            "app out.",
            "de": "Verbinde hier das zweite Konto: Eine Anmeldung mit dem Hauptkonto meldet die "
            "Handy-App ab.",
        },
    ],
    links=[
        ProviderLink(
            label={"en": "Mammotion app", "de": "Mammotion-App"},
            url="https://mammotion.com/pages/app",
        )
    ],
    tested_on_hardware=True,
    mower_options=[
        ConnectionOptionSpec(
            key="session_cache_path",
            type="string",
            title={"en": "Session cache file", "de": "Datei für den Sitzungs-Cache"},
            description={
                "en": "File for the cloud session tokens on a private, writable volume; avoids a "
                "password login on every reconnect.",
                "de": "Datei für die Sitzungs-Tokens der Cloud auf einem privaten, beschreibbaren "
                "Volume; erspart eine Passwort-Anmeldung bei jedem Neuverbinden.",
            },
            advanced=True,
            examples=["/var/lib/device-gateway/mammotion-session.json"],
        ),
        ConnectionOptionSpec(
            key="refresh_seconds",
            type="number",
            title={"en": "State refresh (s)", "de": "Statusabfrage (s)"},
            description={
                "en": "How often the mower is asked for a fresh status report; the Mammotion "
                "cloud sends reports only on request.",
                "de": "Wie oft der Mäher um einen frischen Statusbericht gebeten wird; die "
                "Mammotion-Cloud schickt Berichte nur auf Anfrage.",
            },
            advanced=True,
            minimum=10,
            maximum=300,
            default=20,
        ),
        ConnectionOptionSpec(
            key="refresh_seconds_active",
            type="number",
            title={"en": "State refresh while mowing (s)", "de": "Statusabfrage beim Mähen (s)"},
            description={
                "en": "How often the mower is asked for a fresh status report while it mows, "
                "returns or is paused away from the dock; more requests than the idle refresh, "
                "so more 4G traffic while it works.",
                "de": "Wie oft der Mäher um einen frischen Statusbericht gebeten wird, während "
                "er mäht, zurückfährt oder außerhalb der Station pausiert; mehr Anfragen als im "
                "Ruhezustand, also mehr 4G-Datenverkehr, solange er arbeitet.",
            },
            advanced=True,
            minimum=2,
            maximum=60,
            default=5,
        ),
        ConnectionOptionSpec(
            key="report_stream_seconds",
            type="number",
            title={"en": "Live reports while mowing (s)", "de": "Live-Berichte beim Mähen (s)"},
            description={
                "en": "Length of the continuous report window kept open while the mower works "
                "(renewed every half window); about one report per second, which keeps the live "
                "position smooth. 0 turns it off and uses the plain refresh. Only while mowing, "
                "so no extra 4G traffic while docked.",
                "de": "Länge des durchgehenden Berichtsfensters, das offen bleibt, während der "
                "Mäher arbeitet (alle halbe Fensterlänge erneuert); etwa ein Bericht pro "
                "Sekunde, damit die Live-Position flüssig bleibt. 0 schaltet es ab und nutzt die "
                "einfache Abfrage. Nur beim Mähen, also kein zusätzlicher 4G-Verkehr in der "
                "Station.",
            },
            advanced=True,
            minimum=0,
            maximum=300,
            default=60,
        ),
        ConnectionOptionSpec(
            key="all_cameras",
            type="boolean",
            title={"en": "All cameras", "de": "Alle Kameras"},
            description={
                "en": "Ask the cloud for every camera of the mower (both front cameras of a "
                "LUBA 2 AWD X, the rear one of a Yuka) instead of the front left one only; off "
                "when a mower misbehaves with more than one stream.",
                "de": "Alle Kameras des Mähers von der Cloud anfordern (beide Frontkameras eines "
                "LUBA 2 AWD X, die hintere eines Yuka) statt nur der vorderen linken; aus, wenn "
                "ein Mäher mit mehr als einem Videostrom Probleme macht.",
            },
            advanced=True,
            default=True,
        ),
        ConnectionOptionSpec(
            key="cloud_drive",
            type="boolean",
            title={"en": "Manual driving over the cloud", "de": "Manuelles Fahren über die Cloud"},
            description={
                "en": "Joystick, nudges, AI assist moves and spot runs over the Mammotion cloud "
                "when no Bluetooth bridge is connected (forwards and turns only, about 0.3 s "
                "delay). Needs the mower's owner account; off keeps driving to the bridge.",
                "de": "Joystick, Stupser, KI-Befreiung und Spot-Mähen über die Mammotion-Cloud, "
                "wenn keine Bluetooth-Bridge verbunden ist (nur vorwärts und drehen, etwa 0,3 s "
                "Verzögerung). Braucht das Besitzerkonto des Mähers; aus: Fahren nur über die "
                "Bridge.",
            },
            default=True,
        ),
        ConnectionOptionSpec(
            key="allow_manual_blades",
            type="boolean",
            title={"en": "Blades while driving manually", "de": "Messer beim manuellen Fahren"},
            description={
                "en": "Off keeps the blades off while the mower is driven manually over the cloud.",
                "de": "Aus lässt die Messer aus, solange der Mäher manuell über die Cloud "
                "gefahren wird.",
            },
            default=False,
        ),
        ConnectionOptionSpec(
            key="manual_blade_height_mm",
            type="integer",
            title={
                "en": "Blade height for manual driving (mm)",
                "de": "Schnitthöhe beim manuellen Fahren (mm)",
            },
            advanced=True,
            minimum=20,
            maximum=100,
            default=60,
        ),
        ConnectionOptionSpec(
            key="manual_drive_speed_mps",
            type="number",
            title={
                "en": "Manual driving speed limit (m/s)",
                "de": "Höchstgeschwindigkeit beim manuellen Fahren (m/s)",
            },
            description={
                "en": "max_run_speed sent when a drive session opens; empty: the mower's own "
                "speed setting, at most 1.0.",
                "de": "max_run_speed, das beim Öffnen einer Fahrsitzung gesendet wird; leer: die "
                "eigene Geschwindigkeitseinstellung des Mähers, höchstens 1,0.",
            },
            advanced=True,
            minimum=0.1,
            maximum=1.0,
        ),
        ConnectionOptionSpec(
            key="map_sync_timeout_s",
            type="number",
            title={"en": "Map sync timeout (s)", "de": "Zeitlimit Kartenabgleich (s)"},
            description={
                "en": "How long to wait for the map after connecting.",
                "de": "Wie lange nach dem Verbinden auf die Karte gewartet wird.",
            },
            advanced=True,
            minimum=1,
            maximum=3600,
            default=300,
        ),
        ConnectionOptionSpec(
            key="confirm_timeout_s",
            type="number",
            title={"en": "Confirmation timeout (s)", "de": "Zeitlimit Bestätigung (s)"},
            description={
                "en": "How long a command waits for the mower to confirm it.",
                "de": "Wie lange ein Befehl auf die Bestätigung des Mähers wartet.",
            },
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


def capability_profiles() -> list[CapabilityProfile]:
    """The model families (ADR 0071); see :mod:`mowdeck_control_mammotion.profiles`."""
    from mowdeck_control_mammotion.profiles import capability_profiles as profiles

    return profiles()


SPEC: Final = ProviderSpec(
    provider=PROVIDER,
    create=create_provider,
    enabled_by_default=True,
    unqualified_cloud=True,
    capability_profiles=capability_profiles,
)
"""The entry point object (``mowdeck.cloud_providers``: ``mammotion``)."""

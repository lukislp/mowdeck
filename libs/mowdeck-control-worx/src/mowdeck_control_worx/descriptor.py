"""What the Worx provider asks the user for (ADR 0066): public data, no vendor SDK.

Worx Landroid, Kress and LandXcape mowers share one cloud (Positec), reached through three brand
endpoints. The user enters the e-mail address and the password of the brand's app account and
picks the brand; MowDeck logs in like the app does (pyworxcloud, the app's own OAuth client id,
password grant). No developer account is needed. The cloud API is not documented or supported
by the vendor, which has blocked accounts that log in too often, so the link form says so.
"""

from typing import Final

from mowdeck_contracts.v1 import (
    ConnectionOptionSpec,
    CredentialChoice,
    CredentialField,
    CredentialFieldKind,
    ProviderDescriptor,
)

PROVIDER: Final = "worx"

EMAIL: Final = "email"
"""Credential field: the e-mail address of the app account, the account's identity."""
PASSWORD: Final = "password"  # noqa: S105 - a field key, not a secret
"""Credential field: the password of the app account."""
BRAND: Final = "brand"
"""Credential field: which brand's cloud the account belongs to (:data:`BRANDS`)."""

BRANDS: Final[dict[str, str]] = {
    "worx": "Worx Landroid",
    "kress": "Kress",
    "landxcape": "LandXcape",
}
"""Brand value (pyworxcloud's cloud name) to the label of the brand."""
DEFAULT_BRAND: Final = "worx"

COMMAND_TIMEOUT: Final = "command_timeout_s"
"""Mower option: how long a command waits for the mower's answer over MQTT."""
DEFAULT_COMMAND_TIMEOUT_S: Final = 30.0
MIN_COMMAND_TIMEOUT_S: Final = 5.0
MAX_COMMAND_TIMEOUT_S: Final = 120.0

OFFER_ZONES: Final = "offer_zones"
"""Mower option: list the mower's zones (starting points) as start choices."""

WARNING_UNOFFICIAL: Final = {
    "en": "Unofficial API: MowDeck talks to the cloud the way the brand's app does. The vendor "
    "does not document or support this interface; it can change or stop working at any time.",
    "de": "Inoffizielle API: MowDeck spricht mit der Cloud so wie die App der Marke. Der "
    "Hersteller dokumentiert und unterstützt diese Schnittstelle nicht; sie kann sich jederzeit "
    "ändern oder ausfallen.",
}
WARNING_BAN: Final = {
    "en": "Account block risk: the vendor has temporarily blocked accounts that logged in or "
    "polled too often. MowDeck logs in once per account, lives on push updates and waits after "
    "a refused login, but cannot rule a block out. Prefer a second account the mower is shared "
    "with.",
    "de": "Risiko einer Kontosperre: Der Hersteller hat Konten zeitweise gesperrt, die sich zu "
    "oft angemeldet oder abgefragt haben. MowDeck meldet sich je Konto einmal an, lebt von "
    "Push-Meldungen und wartet nach einer abgelehnten Anmeldung, kann eine Sperre aber nicht "
    "ausschließen. Nimm am besten ein zweites Konto, mit dem der Mäher geteilt ist.",
}

DESCRIPTOR: Final = ProviderDescriptor(
    provider=PROVIDER,
    name="Worx Landroid, Kress, LandXcape",
    summary={
        "en": "Worx Landroid, Kress and LandXcape mowers through their shared cloud (unofficial "
        "API, push over MQTT): state, battery, errors with lift alarms, rain sensor, GPS position "
        "on models with a GPS module; start now, in a zone or along the edge, pause, send home; "
        "rain delay and cutting height where the mower has them. No maps, patterns or manual "
        "driving.",
        "de": "Worx-Landroid-, Kress- und LandXcape-Mäher über ihre gemeinsame Cloud (inoffizielle "
        "API, Push über MQTT): Zustand, Akku, Fehler mit Hebealarm, Regensensor, GPS-Position bei "
        "Modellen mit GPS-Modul; jetzt, in einer Zone oder an der Kante starten, pausieren, nach "
        "Hause schicken; Regenverzögerung und Schnitthöhe, wo der Mäher sie hat. Keine Karten, "
        "Muster oder manuelles Fahren.",
    },
    fields=[
        CredentialField(
            key=BRAND,
            kind=CredentialFieldKind.CHOICE,
            label={"en": "Brand", "de": "Marke"},
            help={
                "en": "The app the mower is registered in.",
                "de": "Die App, in der der Mäher registriert ist.",
            },
            choices=[
                CredentialChoice(value=v, label={"en": label, "de": label})
                for v, label in BRANDS.items()
            ],
            max_length=16,
        ),
        CredentialField(
            key=EMAIL,
            kind=CredentialFieldKind.EMAIL,
            label={"en": "E-mail", "de": "E-Mail"},
            identity=True,
            help={
                "en": "The e-mail address you sign in to the app with.",
                "de": "Die E-Mail-Adresse, mit der du dich in der App anmeldest.",
            },
            placeholder={"en": "name@example.com", "de": "name@example.com"},
            max_length=254,
        ),
        CredentialField(
            key=PASSWORD,
            kind=CredentialFieldKind.SECRET,
            label={"en": "Password", "de": "Passwort"},
            max_length=256,
        ),
    ],
    setup_steps=[
        {
            "en": "Pick the brand of the app your mower is registered in (Worx Landroid, Kress "
            "or LandXcape).",
            "de": "Wähle die Marke der App, in der dein Mäher registriert ist (Worx Landroid, "
            "Kress oder LandXcape).",
        },
        {
            "en": "If the app can share the mower with a second account, create one for MowDeck: "
            "it keeps your phone signed in and limits what a block would affect.",
            "de": "Kann die App den Mäher mit einem zweiten Konto teilen, lege eines für MowDeck "
            "an: Dein Handy bleibt angemeldet, und eine Sperre träfe weniger.",
        },
        {
            "en": "Enter the e-mail address and the password of that account.",
            "de": "Gib die E-Mail-Adresse und das Passwort dieses Kontos ein.",
        },
    ],
    needs_developer_account=False,
    tested_on_hardware=False,
    warnings=[WARNING_UNOFFICIAL, WARNING_BAN],
    mower_options=[
        ConnectionOptionSpec(
            key=OFFER_ZONES,
            type="boolean",
            title={"en": "Offer zones", "de": "Zonen anbieten"},
            description={
                "en": "List the mower's zones (the starting points set in the app) as start "
                "choices next to 'start now'.",
                "de": "Die Zonen des Mähers (die in der App gesetzten Startpunkte) neben "
                "„Jetzt starten“ zur Auswahl anbieten.",
            },
            default=True,
        ),
        ConnectionOptionSpec(
            key=COMMAND_TIMEOUT,
            type="number",
            title={"en": "Command timeout (s)", "de": "Befehls-Zeitlimit (s)"},
            description={
                "en": "How long a command waits for the mower's answer before it counts as failed.",
                "de": "Wie lange ein Befehl auf die Antwort des Mähers wartet, bevor er als "
                "fehlgeschlagen gilt.",
            },
            advanced=True,
            minimum=MIN_COMMAND_TIMEOUT_S,
            maximum=MAX_COMMAND_TIMEOUT_S,
            default=DEFAULT_COMMAND_TIMEOUT_S,
        ),
    ],
)
"""The provider descriptor served to Fleet and the web app."""

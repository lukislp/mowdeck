"""What the Husqvarna provider asks the user for (ADR 0066): public data, no vendor SDK.

The user creates an application in the Husqvarna Developer Portal with their own Husqvarna
account (the one of the Automower Connect app), connects it to the Authentication API and the
Automower Connect API, and enters its application key and application secret here. MowDeck
exchanges them for a bearer token (OAuth2 client-credentials grant) that sees the mowers of that
account. The key is shown on the linked account; the secret is stored encrypted and never shown.
"""

from typing import Final

from mowdeck_contracts.v1 import (
    ConnectionOptionSpec,
    CredentialField,
    CredentialFieldKind,
    ProviderDescriptor,
    ProviderLink,
)

PROVIDER: Final = "husqvarna"

APP_KEY: Final = "app_key"
"""Credential field: the application key (the OAuth2 client id), the account's identity."""
APP_SECRET: Final = "app_secret"  # noqa: S105 - a field key, not a secret
"""Credential field: the application secret (the OAuth2 client secret)."""

START_MINUTES: Final = "start_minutes"
"""Mower option: how long a start without a duration lets the mower mow."""
DEFAULT_START_MINUTES: Final = 60
MAX_START_MINUTES: Final = 7 * 24 * 60

APP_KEY_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9-]{7,63}$"
"""Husqvarna hands out keys as UUIDs; letters, digits and dashes are accepted."""

DEVELOPER_PORTAL: Final = "https://developer.husqvarnagroup.cloud/"

DESCRIPTOR: Final = ProviderDescriptor(
    provider=PROVIDER,
    name="Husqvarna Automower Connect",
    summary={
        "en": "Husqvarna Automower (and Gardena smart) mowers through the official Automower "
        "Connect API: state, battery, errors, GPS position on models with GPS; start for a "
        "duration or in a work area, pause, resume the schedule, park. No maps, patterns or "
        "manual driving.",
        "de": "Husqvarna-Automower- (und Gardena-smart-) Mäher über die offizielle Automower-"
        "Connect-API: Zustand, Akku, Fehler, GPS-Position bei Modellen mit GPS; Start für eine "
        "Dauer oder in einem Arbeitsbereich, Pause, Zeitplan fortsetzen, Parken. Keine Karten, "
        "Muster oder manuelles Fahren.",
    },
    fields=[
        CredentialField(
            key=APP_KEY,
            kind=CredentialFieldKind.TEXT,
            label={"en": "Application key", "de": "Anwendungsschlüssel"},
            identity=True,
            help={
                "en": "The key of your application in the Husqvarna Developer Portal.",
                "de": "Der Schlüssel deiner Anwendung im Husqvarna Developer Portal.",
            },
            placeholder={
                "en": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                "de": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            },
            pattern=APP_KEY_PATTERN,
            max_length=64,
        ),
        CredentialField(
            key=APP_SECRET,
            kind=CredentialFieldKind.SECRET,
            label={"en": "Application secret", "de": "Anwendungsgeheimnis"},
            help={
                "en": "The secret of the same application.",
                "de": "Das Geheimnis derselben Anwendung.",
            },
            max_length=256,
        ),
    ],
    setup_steps=[
        {
            "en": "Sign in to the Husqvarna Developer Portal with your Husqvarna account (the one "
            "of the Automower Connect app).",
            "de": "Melde dich im Husqvarna Developer Portal mit deinem Husqvarna-Konto an (dem "
            "der Automower-Connect-App).",
        },
        {
            "en": "Create an application: any name, any redirect URL such as http://localhost "
            "(MowDeck does not use it).",
            "de": "Lege eine Anwendung an: beliebiger Name, beliebige Weiterleitungs-URL wie "
            "http://localhost (MowDeck nutzt sie nicht).",
        },
        {
            "en": "Connect the application to the Authentication API and the Automower Connect "
            "API.",
            "de": "Verbinde die Anwendung mit der Authentication API und der Automower Connect "
            "API.",
        },
        {
            "en": "Copy the application key and the application secret into this form.",
            "de": "Kopiere Anwendungsschlüssel und Anwendungsgeheimnis in dieses Formular.",
        },
    ],
    links=[
        ProviderLink(
            label={"en": "Husqvarna Developer Portal", "de": "Husqvarna Developer Portal"},
            url=DEVELOPER_PORTAL,
        )
    ],
    needs_developer_account=True,
    tested_on_hardware=False,
    mower_options=[
        ConnectionOptionSpec(
            key=START_MINUTES,
            type="integer",
            title={"en": "Start duration (min)", "de": "Startdauer (min)"},
            description={
                "en": "How long the mower mows after a start that names no duration (a start "
                "for a time or in a work area).",
                "de": "Wie lange der Mäher nach einem Start ohne Dauer mäht (ein Start für eine "
                "Zeit oder in einem Arbeitsbereich).",
            },
            minimum=1,
            maximum=MAX_START_MINUTES,
            default=DEFAULT_START_MINUTES,
        ),
    ],
)
"""The provider descriptor served to Fleet and the web app."""

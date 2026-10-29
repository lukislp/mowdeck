"""What the Husqvarna provider asks the user for (ADR 0065): public data, no vendor SDK.

The user creates an application in the Husqvarna Developer Portal with their own Husqvarna
account (the one of the Automower Connect app), connects it to the Authentication API and the
Automower Connect API, and enters its application key and application secret here. MowDeck
exchanges them for a bearer token (OAuth2 client-credentials grant) that sees the mowers of that
account. The key is shown on the linked account; the secret is stored encrypted and never shown.
"""

from typing import Final

from mowdeck_contracts.v1 import (
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

APP_KEY_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9-]{7,63}$"
"""Husqvarna hands out keys as UUIDs; letters, digits and dashes are accepted."""

DEVELOPER_PORTAL: Final = "https://developer.husqvarnagroup.cloud/"

DESCRIPTOR: Final = ProviderDescriptor(
    provider=PROVIDER,
    name="Husqvarna Automower Connect",
    summary=(
        "Husqvarna Automower (and Gardena smart) mowers through the official Automower Connect "
        "API: state, battery, errors, GPS position on models with GPS; start for a duration or "
        "in a work area, pause, resume the schedule, park. No maps, patterns or manual driving."
    ),
    fields=[
        CredentialField(
            key=APP_KEY,
            kind=CredentialFieldKind.TEXT,
            label="Application key",
            identity=True,
            help="The key of your application in the Husqvarna Developer Portal.",
            placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            pattern=APP_KEY_PATTERN,
            max_length=64,
        ),
        CredentialField(
            key=APP_SECRET,
            kind=CredentialFieldKind.SECRET,
            label="Application secret",
            help="The secret of the same application. Stored encrypted and never shown again.",
            max_length=256,
        ),
    ],
    setup_steps=[
        "Sign in to the Husqvarna Developer Portal with your Husqvarna account (the one of the "
        "Automower Connect app).",
        "Create an application: any name, any redirect URL such as http://localhost (MowDeck "
        "does not use it).",
        "Connect the application to the Authentication API and the Automower Connect API.",
        "Copy the application key and the application secret into this form.",
    ],
    links=[ProviderLink(label="Husqvarna Developer Portal", url=DEVELOPER_PORTAL)],
    needs_developer_account=True,
    tested_on_hardware=False,
)
"""The provider descriptor served to Fleet and the web app."""

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

WARNING_UNOFFICIAL: Final = (
    "Unofficial API: MowDeck talks to the cloud the way the brand's app does. The vendor does "
    "not document or support this interface; it can change or stop working at any time."
)
WARNING_BAN: Final = (
    "Account block risk: the vendor has temporarily blocked accounts that logged in or polled "
    "too often. MowDeck logs in once per account, lives on push updates and waits after a "
    "refused login, but cannot rule a block out. Prefer a second account the mower is shared "
    "with."
)

DESCRIPTOR: Final = ProviderDescriptor(
    provider=PROVIDER,
    name="Worx Landroid, Kress, LandXcape",
    summary=(
        "Worx Landroid, Kress and LandXcape mowers through their shared cloud (unofficial API, "
        "push over MQTT): state, battery, errors with lift alarms, rain sensor, GPS position on "
        "models with a GPS module; start now, in a zone or along the edge, pause, send home; "
        "rain delay and cutting height where the mower has them. No maps, patterns or manual "
        "driving."
    ),
    fields=[
        CredentialField(
            key=BRAND,
            kind=CredentialFieldKind.CHOICE,
            label="Brand",
            help="The app the mower is registered in.",
            choices=[CredentialChoice(value=v, label=label) for v, label in BRANDS.items()],
            max_length=16,
        ),
        CredentialField(
            key=EMAIL,
            kind=CredentialFieldKind.EMAIL,
            label="E-mail",
            identity=True,
            help="The e-mail address you sign in to the app with.",
            placeholder="name@example.com",
            max_length=254,
        ),
        CredentialField(
            key=PASSWORD,
            kind=CredentialFieldKind.SECRET,
            label="Password",
            help="The password of the same account. Stored encrypted and never shown again.",
            max_length=256,
        ),
    ],
    setup_steps=[
        "Pick the brand of the app your mower is registered in (Worx Landroid, Kress or "
        "LandXcape).",
        "If the app can share the mower with a second account, create one for MowDeck: it keeps "
        "your phone signed in and limits what a block would affect.",
        "Enter the e-mail address and the password of that account.",
    ],
    needs_developer_account=False,
    tested_on_hardware=False,
    warnings=[WARNING_UNOFFICIAL, WARNING_BAN],
    mower_options=[
        ConnectionOptionSpec(
            key=OFFER_ZONES,
            type="boolean",
            title="Offer zones",
            description="List the mower's zones (the starting points set in the app) as start "
            "choices next to 'start now'.",
            default=True,
        ),
        ConnectionOptionSpec(
            key=COMMAND_TIMEOUT,
            type="number",
            title="Command timeout (s)",
            description="How long a command waits for the mower's answer before it counts as "
            "failed.",
            advanced=True,
            minimum=MIN_COMMAND_TIMEOUT_S,
            maximum=MAX_COMMAND_TIMEOUT_S,
            default=DEFAULT_COMMAND_TIMEOUT_S,
        ),
    ],
)
"""The provider descriptor served to Fleet and the web app."""

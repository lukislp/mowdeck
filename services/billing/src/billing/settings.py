"""Settings of the billing service.

Every field is read from ``MOWDECK_<NAME>`` or from the file named by ``MOWDECK_<NAME>_FILE``
(the Stripe keys usually as files). The field docstrings are the configuration reference (README
settings table). OpenFGA is configured with the ``MOWDECK_OPENFGA_*`` variables of
``mowdeck-authz``, token verification with the ``MOWDECK_AUTH_*`` variables of ``mowdeck-auth``.

The service only runs when billing is enabled for the installation (Compose profile
``billing``, Helm ``services.billing.enabled``); there is no "off" switch inside it.
"""

from typing import Literal

from mowdeck_service_kit import ServiceSettings
from pydantic import Field, SecretStr


class Settings(ServiceSettings):
    """Settings of billing: the kit settings plus service defaults."""

    service_name: str = Field(
        default="billing", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9\-]*$"
    )
    """Name of the service in logs, metrics and traces."""

    http_port: int = Field(default=8000, ge=1, le=65535)
    """Port the HTTP server listens on."""

    billing_provider: Literal["stripe", "fake"] = "stripe"
    """Payment provider: ``stripe``, or ``fake`` (no network; webhooks in Stripe's format signed
    with ``MOWDECK_BILLING_STRIPE_WEBHOOK_SECRET``, for development and demos)."""

    billing_stripe_secret_key: SecretStr | None = None
    """Stripe secret key (``sk_test_...`` or a restricted ``rk_...``). Required for ``stripe``."""

    billing_stripe_webhook_secret: SecretStr | None = None
    """Signing secret of the webhook endpoint (``whsec_...``); without it every webhook is
    refused with 400."""

    billing_stripe_api_base: str | None = None
    """Other Stripe API base, e.g. ``http://stripe-mock:12111`` for tests; empty: Stripe."""

    billing_automatic_tax: bool = False
    """Stripe Tax: calculate VAT in Checkout (``automatic_tax``). Needs Stripe Tax set up with
    the origin address and the tax registrations (OSS) in the Stripe Dashboard."""

    billing_prices_file: str = "/etc/mowdeck/billing/prices.yaml"
    """The price file (plan key to provider price id, display currency); the image ships
    ``infra/billing/prices.yaml`` at this path. A missing file sells nothing."""

    billing_price_ids: str = ""
    """Overrides of the price file: ``home=price_123,fleet=price_456``."""

    billing_grace_days: int = Field(default=14, ge=0, le=365)
    """Days after the end of the last paid period before a payment problem ends the plan
    (status ``grace`` in Entitlements until then)."""

    billing_app_url: str = ""
    """Public URL of the web app for the return links of Checkout and the portal, e.g.
    ``https://mowdeck.example.org``. Empty: the origin of the request (Traefik's forwarded
    host). Set it in production."""

    billing_entitlements_url: str = "http://entitlements:8000"
    """Base URL of Entitlements for the public plan list (names, trial days). Empty: none."""

    billing_entitlements_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    """Timeout of ``entitlements.plan.set``; without an answer the webhook answers 500 and the
    provider retries."""

    billing_price_cache_seconds: float = Field(default=600, ge=0)
    """How long fetched prices are reused by ``GET /status``."""

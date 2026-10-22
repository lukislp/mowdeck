"""Settings of the vision service.

Every field is read from ``MOWDECK_<NAME>`` or from the file named by ``MOWDECK_<NAME>_FILE``
(the keys usually as files). The field docstrings are the configuration reference (README
settings table). OpenFGA is configured with the ``MOWDECK_OPENFGA_*`` variables of
``mowdeck-authz``, token verification with the ``MOWDECK_AUTH_*`` variables of ``mowdeck-auth``,
the plan checks with the kit's ``MOWDECK_ENTITLEMENTS_*`` variables.

Provider keys of the installation default may also come from the usual variables of the model
family (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ``GEMINI_API_KEY``, ``MISTRAL_API_KEY``):
LiteLLM reads them when ``MOWDECK_VISION_DEFAULT_API_KEY`` is empty.
"""

from typing import Literal

from mowdeck_service_kit import ServiceSettings
from pydantic import Field, SecretStr

from vision.domain.families import DEFAULT_HOSTED_MODEL


class Settings(ServiceSettings):
    """Settings of vision: the kit settings plus service defaults."""

    service_name: str = Field(
        default="vision", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9\-]*$"
    )
    """Name of the service in logs, metrics and traces."""

    http_port: int = Field(default=8000, ge=1, le=65535)
    """Port the HTTP server listens on."""

    # --- providers ------------------------------------------------------------------------

    vision_default_enabled: bool = False
    """Offer the installation's own vision model to users without a provider of their own (plan
    feature ``ai_recover_hosted``). Off by default: every user brings a provider."""

    vision_default_model: str = DEFAULT_HOSTED_MODEL
    """LiteLLM model string of the installation default, e.g. ``anthropic/claude-sonnet-5-5``,
    ``openai/gpt-5-mini``, ``gemini/gemini-2.5-flash``, ``ollama/llava`` or ``fake/recover``
    (the rule based fake, for development and demos)."""

    vision_default_api_base: str | None = None
    """Base URL of the installation default (Ollama, an OpenAI-compatible endpoint, a proxy)."""

    vision_default_api_key: SecretStr | None = None
    """Key of the installation default. Empty: LiteLLM reads the family's usual variable."""

    vision_secrets_key: SecretStr | None = None
    """256-bit key (base64 or hex) that encrypts the users' API keys at rest. Without it users
    can only configure providers without a key (Ollama, open endpoints)."""

    vision_allow_fake_provider: bool = False
    """Development only: users may pick the fake provider (``fake/...``), e.g. in the e2e
    stack."""

    vision_user_api_base_policy: Literal["any", "public", "none"] = "any"
    """Which base URLs users may set: ``any`` (a home installation with Ollama on the LAN),
    ``public`` (no private, loopback or single-label hosts: hosted installations), ``none``."""

    vision_provider_timeout_seconds: float = Field(default=90.0, gt=0, le=600)
    """Timeout of one model call."""

    vision_provider_retries: int = Field(default=2, ge=0, le=5)
    """Retries of a model call after a timeout, a connection error, 429 or 5xx."""

    vision_provider_tests_per_hour: int = Field(default=10, ge=1, le=1000)
    """Provider tests per user and hour."""

    # --- proposals ------------------------------------------------------------------------

    vision_proposal_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    """How long a proposal waits for approval; images are deleted after this time too."""

    vision_max_rounds: int = Field(default=3, ge=1, le=10)
    """Rounds (analysis, approval, execution) per assist session."""

    vision_min_confidence: float = Field(default=0.6, ge=0, le=1)
    """Answers below this confidence are ``rejected_by_ai``."""

    vision_user_daily_requests: int = Field(default=20, ge=0, le=10000)
    """Analyses per user and UTC day (own and hosted providers)."""

    vision_hosted_account_daily_requests: int = Field(default=50, ge=0, le=100000)
    """Analyses with the installation default per account and UTC day."""

    vision_keep_images_days: int = Field(default=0, ge=0, le=365)
    """Keep the images of a proposal this many days longer than its TTL (0: delete with it)."""

    vision_max_images: int = Field(default=4, ge=0, le=8)
    """Camera or uploaded images per request (the map image comes on top, see below)."""

    vision_map_image: bool = True
    """Send a top-down map rendering around the mower (512 px PNG, heading up) as one extra
    image after the camera frames; it never counts against ``vision_max_images``."""

    vision_max_image_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=10 * 1024 * 1024)
    """Size of one uploaded image."""

    vision_context_radius_m: float = Field(default=8.0, gt=0, le=50)
    """Radius of the map excerpt sent to the model."""

    # --- execution ------------------------------------------------------------------------

    vision_frame_rate_hz: float = Field(default=5.0, ge=1, le=10)
    """Drive frames per second while executing (each with ``ttl_ms`` 1000, blades off)."""

    vision_state_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    """Abort when no state arrived for this long."""

    vision_start_state_max_age_seconds: float = Field(default=30.0, gt=0, le=600)
    """Execution starts only with a state at most this old (a resting mower reports every
    heartbeat, 10 s on the Device-Gateway)."""

    vision_geofence_margin_m: float = Field(default=2.0, ge=0, le=20)
    """Margin around the mapped areas automated driving may use."""

    vision_settle_seconds: float = Field(default=1.5, ge=0, le=30)
    """Wait after the last step before the outcome is checked."""

    vision_min_move_m: float = Field(default=0.2, ge=0, le=5)
    """Displacement that counts as "moved as planned"."""

    vision_resume_confirm_seconds: float = Field(default=20.0, gt=0, le=300)
    """After ``resume_mow`` of an approved ``resume``: how long to wait for the mower to report
    ``mowing`` before the outcome says it was sent but not confirmed."""

    vision_linear_mps_at_1: float = Field(default=0.5, gt=0, le=3)
    """Typical speed at ``linear = 1`` (told to the model)."""

    vision_turn_deg_per_s_at_1: float = Field(default=57.0, gt=0, le=360)
    """Typical turn rate at ``angular = 1`` (told to the model)."""

    vision_fleet_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    """Timeout of ``fleet.drive.*`` and ``fleet.commands.issue``."""

    vision_photo_timeout_seconds: float = Field(default=35.0, gt=0, le=120)
    """Timeout of a photo request to the Device-Gateway. A simulator answers at once; a real
    camera through the capture worker (ADR 0035) needs the stream token, the mower to start
    publishing and the first decoded frame, typically 5 to 15 s."""

    vision_dark_frame_threshold: float = Field(default=40.0, ge=0, le=255)
    """A gateway photo whose mean luminance (0..255) is below this is too dark: a mower with a
    switchable light that is off gets the light switched on, the photo is taken again and the
    light switched off after the analysis. 0 disables it."""

    vision_light_wait_seconds: float = Field(default=10.0, ge=0, le=60)
    """How long to wait for the mower to report the light on before the second photo."""

    vision_stereo_max_skew_ms: float = Field(default=500.0, ge=0, le=5000)
    """Two camera frames (``left`` and ``right``) captured further apart than this are analysed
    as separate images, not as a stereo pair (the web app aims at 200 ms)."""
    # --- automatic analysis on errors (ADR 0035) -------------------------------------------

    vision_auto_enabled: bool = True
    """Allow automatic analyses: a user who opted in ("Automatic AI analysis on errors") gets a
    proposal when a mower of the account enters a recoverable error. False switches the
    feature off for the whole installation (the opt-in stays stored)."""

    vision_auto_cooldown_minutes: int = Field(default=10, ge=1, le=1440)
    """No automatic analysis for a mower within this many minutes after the previous one."""

    vision_auto_daily_per_mower: int = Field(default=20, ge=0, le=1000)
    """Automatic analyses per mower and UTC day (0: none). The per-user daily cap
    (``VISION_USER_DAILY_REQUESTS``) applies on top."""

    vision_auto_error_codes: str = (
        "stuck,1032,1100,1145,1201,1206,1214,1218,1411,1421,1422,11112,11113,11121,11122,"
        "11132,11134,2000701"
    )
    """Error codes (comma separated, a leading minus sign is ignored) that start an automatic
    analysis: stuck or trapped (``stuck`` of the simulator, Mammotion 1201, 1421, 11112,
    2000701), blocked by an obstacle (1206, 1214, 11113, 11121, 11122), outside the work area (1100,
    1145, 1411, 1422) and wheel slip (1032, 1218, 11132, 11134). A state with
    ``drive_recoverable`` counts too. Never lift, tilt, battery or blade errors: a state with
    ``raw.lifted`` never starts one."""

    vision_auto_max_state_age_seconds: float = Field(default=120.0, gt=0, le=3600)
    """States older than this (a backlog after a restart) never start an automatic analysis."""

    vision_sweep_seconds: float = Field(default=30.0, ge=1, le=3600)
    """Interval of the sweep (expiry, interrupted work, image purge)."""

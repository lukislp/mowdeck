"""Which vision model a request uses: the user's own provider or the installation's.

Resolution order per request (ADR 0033):

1. the requesting user's own provider, when it is enabled (plan feature ``ai_recover``);
2. else the installation default, when the operator enabled it and the account's plan has
   ``ai_recover_hosted``;
3. else ``409 vision-provider-missing``: the user has to configure a provider.

API keys and extra headers are encrypted with AES-GCM (``MOWDECK_VISION_SECRETS_KEY``, the user
and account as associated data), never returned by the API and decrypted only in memory for one
call.
"""

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import (
    FEATURE_AI_RECOVER_HOSTED,
    Principal,
    VisionTask,
)
from mowdeck_control import flat_png
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.entitlements import Entitlements

from vision.application.ports import Clock, ProviderFactory, SecretBox, Store
from vision.domain.context import Motion, SceneContext
from vision.domain.errors import (
    ConflictError,
    InvalidInput,
    NotFoundError,
    ProviderFailed,
    ProviderMissing,
    QuotaExceeded,
    SecretsUnavailable,
)
from vision.domain.families import (
    FAKE,
    FAMILIES,
    ApiBasePolicy,
    Family,
    check_api_base,
    check_model,
    family_of,
)
from vision.domain.models import (
    Image,
    ProviderConfig,
    ProviderRequest,
    UserPreferences,
    UserProvider,
)

_log = get_logger("vision.providers")

HEADER_NAME_MAX = 64
HEADER_VALUE_MAX = 1000
MAX_HEADERS = 8


@dataclass(frozen=True, slots=True)
class HostedDefault:
    """The installation's own vision model (operator settings)."""

    enabled: bool
    model: str
    api_base: str | None = None
    api_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    """What users may configure."""

    allow_fake: bool = False
    api_base_policy: ApiBasePolicy = "any"
    tests_per_hour: int = 10


@dataclass(frozen=True, slots=True)
class ProviderUpdate:
    """``PUT /me/provider``. ``api_key``/``headers``: None keeps the stored value, an empty
    string or mapping deletes it."""

    model: str
    enabled: bool = True
    api_base: str | None = None
    api_key: str | None = field(default=None, repr=False)
    headers: dict[str, str] | None = field(default=None, repr=False)


Effective = Literal["own", "hosted", "none"]


@dataclass(frozen=True, slots=True)
class ProviderView:
    """What the settings page shows (never a key)."""

    configured: bool
    enabled: bool
    model: str | None
    family: str | None
    api_base: str | None
    api_key_set: bool
    header_names: list[str]
    last_test: dict[str, object] | None
    updated_at: datetime | None
    hosted_available: bool
    hosted_model: str | None
    effective: Effective


@dataclass(frozen=True, slots=True)
class TestResult:
    """Outcome of ``POST /me/provider/test``."""

    ok: bool
    model: str
    family: str
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    detail: str | None


class ProviderService:
    """Use cases of the provider settings and the per-request resolution."""

    def __init__(
        self,
        *,
        store: Store,
        box: SecretBox | None,
        entitlements: Entitlements,
        factory: ProviderFactory,
        hosted: HostedDefault,
        policy: ProviderPolicy,
        clock: Clock,
    ) -> None:
        self.store = store
        self.box = box
        self.entitlements = entitlements
        self.factory = factory
        self.hosted = hosted
        self.policy = policy
        self.clock = clock

    # --- families -------------------------------------------------------------------------

    def families(self) -> list[Family]:
        """The families users may pick (the fake only when the operator allows it)."""
        return [*FAMILIES, FAKE] if self.policy.allow_fake else list(FAMILIES)

    async def hosted_available(self, account_id: UUID) -> bool:
        """The installation default is enabled and the account's plan allows it."""
        if not self.hosted.enabled:
            return False
        reply = await self.entitlements.check_feature(account_id, FEATURE_AI_RECOVER_HOSTED)
        return reply.allowed

    # --- settings -------------------------------------------------------------------------

    async def view(self, principal: Principal) -> ProviderView:
        """The caller's provider settings and the hosted alternative."""
        user, account = _ids(principal)
        own = await self.store.get_provider(user, account)
        hosted = await self.hosted_available(account)
        effective: Effective = (
            "own" if own is not None and own.enabled else ("hosted" if hosted else "none")
        )
        return ProviderView(
            configured=own is not None,
            enabled=own.enabled if own else False,
            model=own.model if own else None,
            family=family_of(own.model) if own else None,
            api_base=own.api_base if own else None,
            api_key_set=own is not None and own.api_key_ciphertext is not None,
            header_names=list(own.header_names) if own else [],
            last_test=own.last_test if own else None,
            updated_at=own.updated_at if own else None,
            hosted_available=hosted,
            hosted_model=self.hosted.model if hosted else None,
            effective=effective,
        )

    async def put(self, principal: Principal, update: ProviderUpdate) -> ProviderView:
        """Create or replace the caller's provider (secrets write-only)."""
        user, account = _ids(principal)
        check_model(update.model, allow_fake=self.policy.allow_fake)
        api_base = check_api_base(update.api_base, self.policy.api_base_policy)
        now = self.clock()
        existing = await self.store.get_provider(user, account)
        key_ct = existing.api_key_ciphertext if existing else None
        headers_ct = existing.headers_ciphertext if existing else None
        header_names = list(existing.header_names) if existing else []
        aad_key, aad_headers = _aad(user, account, "api_key"), _aad(user, account, "headers")
        if update.api_key is not None:
            if update.api_key.strip():
                key_ct = self._encrypt(update.api_key.strip().encode("utf-8"), aad_key)
            else:
                key_ct = None
        if update.headers is not None:
            headers = _check_headers(update.headers)
            if headers:
                payload = json.dumps(headers, sort_keys=True).encode("utf-8")
                headers_ct = self._encrypt(payload, aad_headers)
                header_names = sorted(headers)
            else:
                headers_ct, header_names = None, []
        provider = UserProvider(
            user_id=user,
            account_id=account,
            model=update.model,
            enabled=update.enabled,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            api_base=api_base,
            api_key_ciphertext=key_ct,
            headers_ciphertext=headers_ct,
            header_names=header_names,
            last_test=None,
        )
        await self.store.put_provider(provider)
        _log.info(
            "vision provider saved",
            user_id=str(user),
            model=update.model,
            api_key_set=key_ct is not None,
        )
        return await self.view(principal)

    async def delete(self, principal: Principal) -> None:
        """Delete the caller's provider (404 when there is none)."""
        user, account = _ids(principal)
        if not await self.store.delete_provider(user, account):
            raise NotFoundError("no provider configured")

    # --- preferences ----------------------------------------------------------------------

    async def preferences(self, principal: Principal) -> UserPreferences:
        """The caller's AI assist preferences (the defaults when never saved)."""
        user, account = _ids(principal)
        stored = await self.store.get_preferences(user, account)
        return stored or UserPreferences(user_id=user, account_id=account)

    async def put_preferences(
        self, principal: Principal, *, stereo_frames: bool
    ) -> UserPreferences:
        """Save the caller's preferences."""
        user, account = _ids(principal)
        preferences = UserPreferences(
            user_id=user, account_id=account, stereo_frames=stereo_frames, updated_at=self.clock()
        )
        await self.store.put_preferences(preferences)
        _log.info("vision preferences saved", user_id=str(user), stereo_frames=stereo_frames)
        return preferences

    # --- resolution -----------------------------------------------------------------------

    async def resolve(self, principal: Principal) -> ProviderConfig:
        """The provider of one request (own, else hosted, else ``vision-provider-missing``)."""
        user, account = _ids(principal)
        own = await self.store.get_provider(user, account)
        if own is not None and own.enabled:
            return self._config(own)
        if await self.hosted_available(account):
            return ProviderConfig(
                model=self.hosted.model,
                family=family_of(self.hosted.model),
                hosted=True,
                api_base=self.hosted.api_base,
                api_key=self.hosted.api_key,
            )
        raise ProviderMissing(
            "no vision provider is available: configure your own under Settings, AI provider"
        )

    def _config(self, own: UserProvider) -> ProviderConfig:
        aad_key = _aad(own.user_id, own.account_id, "api_key")
        aad_headers = _aad(own.user_id, own.account_id, "headers")
        api_key = None
        headers: dict[str, str] = {}
        try:
            if own.api_key_ciphertext is not None:
                api_key = self._decrypt(own.api_key_ciphertext, aad_key).decode("utf-8")
            if own.headers_ciphertext is not None:
                headers = json.loads(self._decrypt(own.headers_ciphertext, aad_headers))
        except ValueError as exc:
            msg = "the stored API key cannot be decrypted any more: enter it again"
            raise ConflictError(msg) from exc
        return ProviderConfig(
            model=own.model,
            family=family_of(own.model),
            hosted=False,
            api_base=own.api_base,
            api_key=api_key,
            headers=headers,
        )

    # --- test -----------------------------------------------------------------------------

    async def test(self, principal: Principal) -> TestResult:
        """A minimal vision call with a tiny generated image against the caller's provider."""
        user, account = _ids(principal)
        own = await self.store.get_provider(user, account)
        if own is None:
            raise ProviderMissing("configure a provider before testing it")
        now = self.clock()
        used = await self.store.count_requests(
            since=now - timedelta(hours=1), kind="test", user_id=user
        )
        if used >= self.policy.tests_per_hour:
            raise QuotaExceeded(f"at most {self.policy.tests_per_hour} provider tests per hour")
        config = self._config(own)
        provider = self.factory.create(config)
        image = Image(flat_png(64, 48, (74, 140, 66)), "image/png", "upload")
        context = SceneContext(
            task=VisionTask.RECOVER,
            motion=Motion(linear_mps_at_1=0.5, turn_deg_per_s_at_1=57.0),
            images=1,
        )
        started = time.monotonic()
        outcome, detail = "ok", None
        usage = None
        try:
            assessment = await provider.analyse([image], context, VisionTask.RECOVER)
            usage = assessment.usage
        except ProviderFailed as exc:
            outcome, detail = "error", exc.message
        latency = usage.latency_ms if usage else round((time.monotonic() - started) * 1000)
        await self.store.record_request(
            ProviderRequest(
                id=uuid4(),
                account_id=account,
                user_id=user,
                kind="test",
                model=config.model,
                family=config.family,
                hosted=False,
                outcome=outcome,
                created_at=now,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                cost_usd=usage.cost_usd if usage else None,
                latency_ms=latency,
                detail=detail,
            )
        )
        result = TestResult(
            ok=outcome == "ok",
            model=config.model,
            family=config.family,
            latency_ms=latency,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            cost_usd=usage.cost_usd if usage else None,
            detail=detail,
        )
        own.last_test = {
            "at": now.isoformat(),
            "ok": result.ok,
            "latency_ms": result.latency_ms,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd,
            "detail": detail,
        }
        await self.store.put_provider(own)
        return result

    # --- secrets --------------------------------------------------------------------------

    def _encrypt(self, plaintext: bytes, aad: str) -> bytes:
        if self.box is None:
            raise SecretsUnavailable(
                "API keys cannot be stored: the operator has not set MOWDECK_VISION_SECRETS_KEY"
            )
        return self.box.encrypt(plaintext, aad)

    def _decrypt(self, ciphertext: bytes, aad: str) -> bytes:
        if self.box is None:
            msg = "no secrets key configured"
            raise ValueError(msg)
        return self.box.decrypt(ciphertext, aad)


def _ids(principal: Principal) -> tuple[UUID, UUID]:
    if principal.account_id is None:
        raise InvalidInput("this operation needs an account-scoped principal")
    return principal.subject, principal.account_id


def _aad(user_id: UUID, account_id: UUID, what: str) -> str:
    return f"vision:{account_id}:{user_id}:{what}"


def _check_headers(headers: dict[str, str]) -> dict[str, str]:
    if len(headers) > MAX_HEADERS:
        raise InvalidInput(f"at most {MAX_HEADERS} extra headers")
    result: dict[str, str] = {}
    for name, value in headers.items():
        clean = name.strip()
        if (
            not clean
            or len(clean) > HEADER_NAME_MAX
            or not all(c.isalnum() or c in "-_" for c in clean)
        ):
            raise InvalidInput(f"invalid header name {name[:HEADER_NAME_MAX]!r}")
        if len(value) > HEADER_VALUE_MAX or "\n" in value or "\r" in value:
            raise InvalidInput(f"invalid value of header {clean!r}")
        result[clean] = value
    return result

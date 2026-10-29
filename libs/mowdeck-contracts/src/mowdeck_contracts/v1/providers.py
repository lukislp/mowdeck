"""Cloud providers: what a vendor adapter declares about linking an account (ADR 0066).

Every adapter package that connects mowers through a vendor cloud registers a provider plugin
in the Device-Gateway (entry point group ``mowdeck.cloud_providers``, see
``mowdeck_control.providers``). The plugin describes itself with a :class:`ProviderDescriptor`:
its id, display name, the credential fields the user must enter (with short guidance and links
to where the vendor hands them out), and whether it was ever tested on real hardware. Fleet
asks the gateway for the enabled providers on :data:`~mowdeck_contracts.v1.subjects.\
GATEWAY_CLOUD_PROVIDERS`, validates a link request against the descriptor
(:meth:`ProviderDescriptor.problems`) and serves the descriptors to the web app, which renders
the link form from them. No core service knows a vendor by name.

Every text a user reads (summary, setup steps, link labels, field labels and help, choice
labels, option titles and help) is a :data:`LocalizedText`: a map of locale to text with at
least English; a plain string is taken as English. Fleet resolves the texts for the caller's
language (:func:`text_for`, English fallback). The display ``name`` is the vendor's brand and
is not translated. Generic texts (that a secret is stored encrypted, the untested label) come
from the web app's own messages, not from each provider.

Descriptors never hold credentials; they are public data.
"""

import re
from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, BeforeValidator, Field, StringConstraints, model_validator

from mowdeck_contracts.v1._base import ContractModel
from mowdeck_contracts.v1.error_lexicon import DEFAULT_LOCALE, LocaleKey, normalise_locale


def _as_localized(value: object) -> object:
    return {DEFAULT_LOCALE: value} if isinstance(value, str) else value


def _with_english(value: dict[str, str]) -> dict[str, str]:
    if not value.get(DEFAULT_LOCALE, "").strip():
        msg = f"a localized text needs a non-empty {DEFAULT_LOCALE!r} text"
        raise ValueError(msg)
    return value


LocalizedText = Annotated[
    dict[LocaleKey, Annotated[str, StringConstraints(max_length=500)]],
    BeforeValidator(_as_localized),
    AfterValidator(_with_english),
]
"""A user-visible text by locale (``{"en": ..., "de": ...}``); English is required, a plain
string is taken as English."""


def text_for(value: Mapping[str, str] | None, locale: str | None) -> str | None:
    """``value`` in ``locale`` (``de-DE`` gives ``de``), else English; None for no text."""
    if not value:
        return None
    return value.get(normalise_locale(locale)) or value.get(DEFAULT_LOCALE)


PROVIDER_ID_PATTERN: Final = r"^[a-z][a-z0-9_-]{1,31}$"
"""Provider ids: lower case, stable, used as the entry point name and stored with every linked
account (``linked_accounts.provider``)."""
CREDENTIAL_KEY_PATTERN: Final = r"^[a-z][a-z0-9_]{0,39}$"
"""Keys of credential fields (the names under which the values reach the adapter)."""

MAX_CREDENTIAL_LENGTH: Final = 4096
"""Upper bound of any credential value (long enough for a pasted token)."""


class CredentialFieldKind(StrEnum):
    """How a credential field is entered and stored."""

    TEXT = "text"
    """Plain text, stored as is and shown on the linked account (e.g. an application key)."""
    EMAIL = "email"
    """An e-mail address (checked for an ``@``), stored and shown."""
    SECRET = "secret"  # noqa: S105 - a field kind, not a password
    """Write-only: stored encrypted, never returned, never logged (passwords, secrets)."""
    CHOICE = "choice"
    """One of ``choices`` (e.g. the brand of a vendor cloud), stored and shown."""


class CredentialChoice(ContractModel):
    """One option of a ``choice`` field."""

    value: str = Field(min_length=1, max_length=64)
    label: LocalizedText


class CredentialField(ContractModel):
    """One input of a provider's link form."""

    key: str = Field(pattern=CREDENTIAL_KEY_PATTERN)
    kind: CredentialFieldKind
    label: LocalizedText = Field(description="Label of the input by locale.")
    help: LocalizedText | None = Field(
        default=None, description="Provider-specific hint under the input by locale."
    )
    placeholder: LocalizedText | None = None
    required: bool = True
    identity: bool = Field(
        default=False,
        description="The field that names the vendor account (exactly one per provider): "
        "shown as the linked account's title and unique per provider and MowDeck account, "
        "compared without case.",
    )
    choices: list[CredentialChoice] = Field(default_factory=list, max_length=20)
    pattern: str | None = Field(
        default=None, max_length=200, description="Regular expression the value must match."
    )
    max_length: int = Field(default=256, ge=1, le=MAX_CREDENTIAL_LENGTH)

    @property
    def secret(self) -> bool:
        """Write-only and stored encrypted."""
        return self.kind is CredentialFieldKind.SECRET

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.identity and self.kind not in (CredentialFieldKind.TEXT, CredentialFieldKind.EMAIL):
            msg = "the identity field must be a text or e-mail field"
            raise ValueError(msg)
        if (self.kind is CredentialFieldKind.CHOICE) != bool(self.choices):
            msg = "choices are needed by (and only allowed for) choice fields"
            raise ValueError(msg)
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                msg = f"pattern is no regular expression: {exc}"
                raise ValueError(msg) from None
        return self


class ProviderLink(ContractModel):
    """A page of the vendor that explains where the credentials come from."""

    label: LocalizedText
    url: str = Field(pattern=r"^https://\S+$", max_length=300)


class CredentialProblem(ContractModel):
    """A credential value that does not fit its field."""

    key: str
    code: str = Field(description="`required`, `unknown`, `too_long`, `format` or `choice`.")
    message: str


RESERVED_OPTION_KEYS: Final = frozenset(
    {
        "device_name",
        "iot_id",
        "cloud_provider",
        "linked_account_id",
        "credentials",
        "credentials_rev",
        "links",
        "base_station",
        "drive_full_speed_mps",
        "drive_full_yaw_rate_dps",
        "drive_dead_zone",
        "drive_turn_dead_zone",
        "drive_speed_setting",
        "calibrated_at",
    }
)
"""Option keys MowDeck sets on every cloud mower (the device of the linked account, its
provider, the credential bookkeeping, links, the base station and the drive calibration). A
provider's credential fields and mower options must not use them: the gateway merges the
credentials into the connection options, so a collision would overwrite a managed value."""

OptionType = Literal["string", "integer", "number", "boolean"]


class ConnectionOptionSpec(ContractModel):
    """One option a provider's cloud mowers take (refresh intervals, a feature switch, ...).

    Fleet validates a cloud mower's options against the options its provider declares and the
    web app renders them on the mower's settings page; only declared options exist, so a vendor
    with few settings shows few.
    """

    key: str = Field(pattern=CREDENTIAL_KEY_PATTERN)
    type: OptionType
    title: LocalizedText = Field(description="Title by locale.")
    description: LocalizedText | None = Field(default=None, description="Help text by locale.")
    advanced: bool = Field(default=False, description="Rarely needed; shown under 'advanced'.")
    minimum: float | None = None
    maximum: float | None = None
    default: str | int | float | bool | None = None
    examples: list[str] = Field(default_factory=list, max_length=5)


class ProviderDescriptor(ContractModel):
    """A cloud provider as its adapter declares it: the link form, the guidance and the options
    of its mowers."""

    provider: str = Field(pattern=PROVIDER_ID_PATTERN)
    name: str = Field(min_length=1, max_length=80, description="Display name of the vendor cloud.")
    summary: LocalizedText | None = Field(
        default=None, description="Which mowers and what MowDeck can do, by locale."
    )
    fields: list[CredentialField] = Field(min_length=1, max_length=10)
    setup_steps: list[LocalizedText] = Field(
        default_factory=list,
        max_length=8,
        description="Short steps by locale: how the user gets the credentials, in order.",
    )
    links: list[ProviderLink] = Field(default_factory=list, max_length=5)
    needs_developer_account: bool = Field(
        default=False,
        description="The user creates an application in the vendor's developer portal (with "
        "their own vendor account) to get the credentials.",
    )
    tested_on_hardware: bool = Field(
        default=False,
        description="False: built and tested against recorded fixtures and fakes only; the web "
        "app labels such providers as untested on real hardware.",
    )
    mower_options: list[ConnectionOptionSpec] = Field(
        default_factory=list,
        max_length=30,
        description="The options a mower of this provider takes (none: the provider has no "
        "settings beyond the linked account).",
    )

    @model_validator(mode="after")
    def _one_identity(self) -> Self:
        keys = [f.key for f in self.fields]
        if len(set(keys)) != len(keys):
            msg = "credential field keys must be unique"
            raise ValueError(msg)
        options = [o.key for o in self.mower_options]
        if len(set(options)) != len(options):
            msg = "mower option keys must be unique"
            raise ValueError(msg)
        clash = (set(keys) | set(options)) & RESERVED_OPTION_KEYS
        if clash:
            msg = f"reserved option keys cannot be used: {', '.join(sorted(clash))}"
            raise ValueError(msg)
        if set(keys) & set(options):
            msg = "a mower option cannot have the key of a credential field"
            raise ValueError(msg)
        if sum(1 for f in self.fields if f.identity) != 1:
            msg = "exactly one credential field must be the identity"
            raise ValueError(msg)
        if not any(f.secret for f in self.fields):
            msg = "a provider needs at least one secret field"
            raise ValueError(msg)
        return self

    def texts(self) -> Iterator[tuple[str, Mapping[str, str]]]:
        """Every user-visible text with where it is (for checks that each has a locale)."""
        if self.summary:
            yield "summary", self.summary
        for index, step in enumerate(self.setup_steps):
            yield f"setup_steps[{index}]", step
        for index, link in enumerate(self.links):
            yield f"links[{index}].label", link.label
        for field in self.fields:
            yield f"fields.{field.key}.label", field.label
            if field.help:
                yield f"fields.{field.key}.help", field.help
            if field.placeholder:
                yield f"fields.{field.key}.placeholder", field.placeholder
            for choice in field.choices:
                yield f"fields.{field.key}.choices.{choice.value}", choice.label
        for option in self.mower_options:
            yield f"mower_options.{option.key}.title", option.title
            if option.description:
                yield f"mower_options.{option.key}.description", option.description

    @property
    def identity_field(self) -> CredentialField:
        """The field that names the vendor account."""
        return next(f for f in self.fields if f.identity)

    @property
    def secret_keys(self) -> frozenset[str]:
        """Keys of the write-only fields."""
        return frozenset(f.key for f in self.fields if f.secret)

    def normalized(self, credentials: Mapping[str, str]) -> dict[str, str]:
        """``credentials`` with text, e-mail and choice values stripped (secrets as entered)
        and empty optional values dropped."""
        by_key = {f.key: f for f in self.fields}
        out: dict[str, str] = {}
        for key, value in credentials.items():
            field = by_key.get(key)
            clean = value if field is None or field.secret else value.strip()
            if clean:
                out[key] = clean
        return out

    def problems(
        self, credentials: Mapping[str, str], *, only_secrets: bool = False
    ) -> list[CredentialProblem]:
        """What is wrong with ``credentials`` (already :meth:`normalized`); empty when they fit.

        ``only_secrets``: a re-authentication, which may carry nothing but secret fields (all of
        them optional; the stored values stay for the missing ones).
        """
        by_key = {f.key: f for f in self.fields}
        found: list[CredentialProblem] = []
        for key in credentials:
            field = by_key.get(key)
            if field is None or (only_secrets and not field.secret):
                found.append(CredentialProblem(key=key, code="unknown", message="unknown field"))
        for field in self.fields:
            value = credentials.get(field.key)
            if value is None:
                if field.required and not only_secrets:
                    found.append(
                        CredentialProblem(key=field.key, code="required", message="required")
                    )
                continue
            if only_secrets and not field.secret:
                continue
            found.extend(_value_problems(field, value))
        return found


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]{2,}$")


def _is_email(value: str) -> bool:
    """One ``@``, a dotted domain, at most 254 characters, no blanks or control characters."""
    if len(value) > 254 or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False
    return _EMAIL.match(value) is not None


def _value_problems(field: CredentialField, value: str) -> list[CredentialProblem]:
    if len(value) > field.max_length:
        message = f"at most {field.max_length} characters"
        return [CredentialProblem(key=field.key, code="too_long", message=message)]
    if field.kind is CredentialFieldKind.EMAIL and not _is_email(value):
        return [CredentialProblem(key=field.key, code="format", message="not an e-mail address")]
    if field.kind is CredentialFieldKind.CHOICE and value not in {c.value for c in field.choices}:
        return [CredentialProblem(key=field.key, code="choice", message="not one of the choices")]
    if field.pattern is not None and not re.fullmatch(field.pattern, value):
        return [CredentialProblem(key=field.key, code="format", message="unexpected format")]
    return []


class CloudProvidersRequest(ContractModel):
    """Request on ``gateway.cloud.providers`` (internal, Fleet to Device-Gateway)."""


class CloudProvidersReply(ContractModel):
    """The cloud providers the gateway has installed and enabled, sorted by name."""

    providers: list[ProviderDescriptor] = Field(default_factory=list, max_length=50)

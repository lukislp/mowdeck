"""WebAuthn ceremonies with py_webauthn (``webauthn`` on PyPI, Duo Labs, BSD-3-Clause).

The library builds the option sets and verifies the responses (challenge, origin, RP ID hash,
user presence and verification, attestation format, signature). It is used with
``attestation: none``: MowDeck does not restrict which authenticators users may use, so there is
no metadata service and no attestation trust store.

Every library error becomes :class:`CeremonyRejected`; malformed input (bad base64url, broken
CBOR) can raise other exceptions inside the library, which are caught the same way.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    parse_authentication_credential_json,
    parse_registration_credential_json,
)
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from identity.application.ports import (
    AssertionClaim,
    CeremonyRejected,
    KnownCredential,
    NewCredential,
    VerifiedAssertion,
)
from identity.domain.models import RelyingParty

_ZERO_AAGUID = "00000000-0000-0000-0000-000000000000"
_TRANSPORTS = {t.value for t in AuthenticatorTransport}


def _transports(values: Sequence[str]) -> list[AuthenticatorTransport]:
    return [AuthenticatorTransport(v) for v in values if v in _TRANSPORTS]


def _as_dict(options: Any) -> dict[str, Any]:
    loaded = json.loads(options_to_json(options))
    assert isinstance(loaded, dict)  # noqa: S101 - options always serialise to an object
    return loaded


class PyWebAuthnCeremonies:
    """Implements ``PasskeyCeremonies`` on py_webauthn."""

    def registration_options(
        self,
        rp: RelyingParty,
        *,
        challenge: bytes,
        user_handle: bytes,
        user_name: str,
        display_name: str,
        exclude: Sequence[KnownCredential],
        timeout_ms: int,
    ) -> dict[str, Any]:
        """Discoverable credential, user verification required, no attestation."""
        options = generate_registration_options(
            rp_id=rp.id,
            rp_name=rp.name,
            user_id=user_handle,
            user_name=user_name,
            user_display_name=display_name,
            challenge=challenge,
            timeout=timeout_ms,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                require_resident_key=True,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(
                    id=known.credential_id, transports=_transports(known.transports) or None
                )
                for known in exclude
            ],
        )
        return _as_dict(options)

    def verify_registration(
        self, rp: RelyingParty, *, credential: Mapping[str, Any], challenge: bytes
    ) -> NewCredential:
        """Verify the attestation response; user verification is required."""
        try:
            parsed = parse_registration_credential_json(dict(credential))
            verified = verify_registration_response(
                credential=parsed,
                expected_challenge=challenge,
                expected_rp_id=rp.id,
                expected_origin=list(rp.origins),
                require_user_presence=True,
                require_user_verification=True,
            )
        except Exception as exc:
            raise CeremonyRejected(f"registration rejected: {exc}") from exc
        transports = tuple(t.value for t in parsed.response.transports or [])
        aaguid = verified.aaguid if verified.aaguid and verified.aaguid != _ZERO_AAGUID else None
        return NewCredential(
            credential_id=verified.credential_id,
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            transports=transports,
            aaguid=aaguid,
            backup_eligible=verified.credential_device_type.value == "multi_device",
            backed_up=verified.credential_backed_up,
        )

    def authentication_options(
        self, rp: RelyingParty, *, challenge: bytes, timeout_ms: int
    ) -> dict[str, Any]:
        """No ``allowCredentials``: the authenticator offers its discoverable credentials."""
        options = generate_authentication_options(
            rp_id=rp.id,
            challenge=challenge,
            timeout=timeout_ms,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return _as_dict(options)

    def read_assertion(self, credential: Mapping[str, Any]) -> AssertionClaim:
        """The credential id and user handle of an authentication response (unverified)."""
        try:
            parsed = parse_authentication_credential_json(dict(credential))
        except Exception as exc:
            raise CeremonyRejected(f"malformed authentication response: {exc}") from exc
        return AssertionClaim(parsed.raw_id, parsed.response.user_handle)

    def verify_assertion(
        self,
        rp: RelyingParty,
        *,
        credential: Mapping[str, Any],
        challenge: bytes,
        public_key: bytes,
    ) -> VerifiedAssertion:
        """Verify the signature; the counter is compared by the caller (stored count 0 here)."""
        try:
            verified = verify_authentication_response(
                credential=dict(credential),
                expected_challenge=challenge,
                expected_rp_id=rp.id,
                expected_origin=list(rp.origins),
                credential_public_key=public_key,
                credential_current_sign_count=0,
                require_user_verification=True,
            )
        except Exception as exc:
            raise CeremonyRejected(f"authentication rejected: {exc}") from exc
        return VerifiedAssertion(verified.new_sign_count, verified.credential_backed_up)

"""Usernames, passwords and opaque secrets (pure functions, standard library only).

* Usernames: 3 to 64 characters, NFKC normalised, compared case-insensitively; letters, digits
  and ``. _ -``. Unique per installation.
* Passwords: 10 to 256 characters, no composition rules, not on a small list of common
  passwords (OWASP ASVS style).
* Refresh tokens ``mdr_<43 chars>`` and API tokens ``mdk_<token id hex>_<43 chars>`` carry 256
  random bits. Only their SHA-256 is stored; a slow hash is not needed for random secrets.
* App login codes ``mda_<43 chars>`` (passkey login handed from the system browser to the native
  app) carry 256 random bits as well and are bound to a PKCE ``S256`` code challenge.
* Passkey names: 1 to 64 printable characters.
"""

import base64
import hashlib
import hmac
import re
import secrets
import unicodedata
from uuid import UUID

from identity.domain.errors import InvalidInput

USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256

REFRESH_PREFIX = "mdr_"
API_TOKEN_PREFIX = "mdk_"  # noqa: S105 - token prefix, not a secret
APP_CODE_PREFIX = "mda_"
_TOKEN_ID_HEX = re.compile(r"[0-9a-f]{32}")
_BASE64URL_43 = re.compile(r"[A-Za-z0-9_-]{43}")
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")
"""RFC 7636 section 4.1: 43 to 128 unreserved characters."""
MAX_PASSKEY_NAME_LENGTH = 64

COMMON_PASSWORDS = frozenset(
    {
        "1234567890",
        "12345678910",
        "0123456789",
        "1111111111",
        "0000000000",
        "1q2w3e4r5t",
        "qwertyuiop",
        "qwertzuiop",
        "password12",
        "password123",
        "password1234",
        "passwort123",
        "iloveyou123",
        "abcdefghij",
        "letmein123",
        "welcome123",
        "changeme123",
        "administrator",
        "mowdeck123",
        "mowdeckmowdeck",
        "rasenmaeher",
        "lawnmower123",
    }
)
"""A deliberately small list; operators can add a breached-password check later."""


def normalize_username(username: str) -> str:
    """NFKC, trimmed, lower case."""
    return unicodedata.normalize("NFKC", username).strip().casefold()


def validate_username(username: str) -> str:
    """The normalised username, or ``InvalidInput``."""
    norm = normalize_username(username)
    if not USERNAME_PATTERN.match(norm):
        msg = "usernames have 3 to 64 characters: letters, digits, '.', '_' or '-'"
        raise InvalidInput(msg)
    return norm


def normalize_email(email: str | None) -> str | None:
    """Lower-cased, trimmed e-mail, or None."""
    if email is None or not email.strip():
        return None
    value = unicodedata.normalize("NFKC", email).strip()
    if "@" not in value or len(value) > 254 or value.startswith("@") or value.endswith("@"):
        msg = "invalid e-mail address"
        raise InvalidInput(msg)
    return value.casefold()


def validate_password(password: str, *, username: str | None = None) -> None:
    """``InvalidInput`` unless the password satisfies the policy."""
    if len(password) < MIN_PASSWORD_LENGTH:
        msg = f"passwords need at least {MIN_PASSWORD_LENGTH} characters"
        raise InvalidInput(msg)
    if len(password) > MAX_PASSWORD_LENGTH:
        msg = f"passwords have at most {MAX_PASSWORD_LENGTH} characters"
        raise InvalidInput(msg)
    lowered = password.casefold()
    if lowered in COMMON_PASSWORDS or (username and lowered == normalize_username(username)):
        msg = "this password is too common"
        raise InvalidInput(msg)


def sha256_hex(secret: str) -> str:
    """SHA-256 of a secret, hex encoded."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def same_hash(expected: str, secret: str) -> bool:
    """Constant-time comparison of a stored hash with the hash of ``secret``."""
    return hmac.compare_digest(expected, sha256_hex(secret))


def new_refresh_token() -> str:
    """A new opaque refresh token."""
    return REFRESH_PREFIX + secrets.token_urlsafe(32)


def new_api_token(token_id: UUID) -> tuple[str, str]:
    """``(token, secret_part)`` for a new API token with id ``token_id``."""
    secret = secrets.token_urlsafe(32)
    return f"{API_TOKEN_PREFIX}{token_id.hex}_{secret}", secret


def parse_api_token(token: str) -> tuple[UUID, str] | None:
    """``(token_id, secret_part)`` of a well-formed API token, else None."""
    if not token.startswith(API_TOKEN_PREFIX):
        return None
    body = token[len(API_TOKEN_PREFIX) :]
    token_hex, sep, secret = body.partition("_")
    # Exactly the spelling new_api_token writes: UUID() alone also takes a sign, whitespace
    # and upper case, so several strings would name the same token.
    if not sep or not _TOKEN_ID_HEX.fullmatch(token_hex) or len(secret) < 32:
        return None
    return UUID(hex=token_hex), secret


def slugify(name: str, suffix: str) -> str:
    """A URL friendly slug for an account name, made unique with ``suffix``."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "-", ascii_name.casefold()).strip("-")[:40] or "account"
    return f"{base}-{suffix}"


def new_app_code() -> str:
    """A new one-time app login code."""
    return APP_CODE_PREFIX + secrets.token_urlsafe(32)


def s256_challenge(verifier: str) -> str:
    """``BASE64URL(SHA256(verifier))`` without padding (RFC 7636 ``S256``)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def validate_code_challenge(challenge: str) -> str:
    """The ``S256`` code challenge (43 base64url characters), or ``InvalidInput``."""
    if not _BASE64URL_43.fullmatch(challenge):
        msg = "code_challenge must be the base64url S256 hash of the verifier"
        raise InvalidInput(msg)
    return challenge


def verifier_matches(challenge: str, verifier: str) -> bool:
    """True if ``verifier`` is well formed and hashes to ``challenge`` (constant time)."""
    if not _VERIFIER.fullmatch(verifier):
        return False
    return hmac.compare_digest(challenge, s256_challenge(verifier))


def validate_passkey_name(name: str) -> str:
    """A trimmed passkey name of 1 to 64 printable characters, or ``InvalidInput``."""
    value = unicodedata.normalize("NFKC", name).strip()
    if not value or len(value) > MAX_PASSKEY_NAME_LENGTH or not value.isprintable():
        msg = f"passkey names have 1 to {MAX_PASSKEY_NAME_LENGTH} printable characters"
        raise InvalidInput(msg)
    return value

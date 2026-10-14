# mowdeck-auth

Access tokens for MowDeck: Identity signs short-lived EdDSA JWTs and every service verifies them locally against Identity's public keys (JWKS), without a call to Identity per request. Licence: Apache-2.0. Depends on `mowdeck-contracts`, `mowdeck-service-kit`, `PyJWT[crypto]` (2.15.1 tested), `cryptography` and `httpx`.

Decision record: [ADR 0015](../../docs/adr/0015-tokens-sessions-and-key-rotation.md). Design: [identity and authorization](../../docs/dev/design-identity-authz.md), section 3.5.

| Module | Content |
| --- | --- |
| `verifier` | `JwtVerifier` (`verify()` gives the principal, `verify_token()` a `VerifiedToken` with the principal and `expires_at`), `VerifiedToken`, `TokenError` |
| `jwks` | `KeySource` protocol, `JwksCache` (HTTP, cached), `StaticKeySource`, `parse_jwks()` |
| `resolver` | `JwtPrincipalResolver` (the kit's `PrincipalResolver`), `principal_resolver_from_env()`, `install_auth()`, `bearer_token()` |
| `issuer` | `TokenIssuer`, `IssuedToken` (Identity and tests) |
| `keys` | `SigningKey` (Ed25519), `public_jwk()`, `thumbprint()` (RFC 7638 kid) |
| `claims` | `principal_claims()`, `principal_from_claims()` |
| `settings` | `AuthSettings.from_env()` |
| `testing` | `TestKeys` (key, `mint()`, fake JWKS endpoint, verifier, resolver), `bearer()` |

## Use in a service

```python
from mowdeck_auth import principal_resolver_from_env
from mowdeck_service_kit import create_app

app = create_app(
    settings, DESCRIPTOR, routers=[router], principal_resolver=principal_resolver_from_env()
)
```

`principal_resolver_from_env()` returns None when `MOWDECK_AUTH_JWKS_URL` is unset (the kit then answers 401 everywhere). Handlers use the kit's `CurrentPrincipal`, `CurrentAccountId` and `require_permission(...)`, and `mowdeck_authz.fastapi.require(...)` for objects.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MOWDECK_AUTH_JWKS_URL` | unset | Identity's JWKS; inside Compose `http://identity:8000/.well-known/jwks.json` |
| `MOWDECK_AUTH_ISSUER` | `urn:mowdeck:identity` | Expected `iss` (Identity's `MOWDECK_IDENTITY_ISSUER`) |
| `MOWDECK_AUTH_AUDIENCE` | `mowdeck-api` | Expected `aud` |
| `MOWDECK_AUTH_LEEWAY_SECONDS` | `30` | Clock skew for `exp`, `nbf`, `iat` |
| `MOWDECK_AUTH_JWKS_CACHE_SECONDS` | `300` | Lifetime of a fetched key set |
| `MOWDECK_AUTH_JWKS_REFRESH_COOLDOWN_SECONDS` | `30` | Minimum time between two fetches |

## What is verified

In this order; the first failure raises `TokenError` (reason for logs only) and the resolver answers **401** with `WWW-Authenticate: Bearer error="invalid_token"` and one generic message:

1. At most 8 KiB, a JWT with a JSON header.
2. `alg` in the allow list: only `EdDSA` (RFC 8037). `none` and HMAC are refused, also in the constructor, so the public key can never act as an HMAC secret. PyJWT 2.15.1 does not implement the fully specified `Ed25519` name of RFC 9864 (checked); it is not accepted until it does.
3. `typ` is `at+jwt` (RFC 9068), a `kid` is present.
4. The key for `kid` is known and is an OKP Ed25519 key.
5. Signature, `iss`, `aud`, `exp`, `nbf`, `iat` (leeway 30 s); `sub`, `kind`, `iss`, `aud`, `iat`, `exp`, `jti` are required.
6. The claims map to a valid `Principal`: `sub` to `subject`, `acc` to `account_id`, `kind`, `name` to `display_name`, `roles`, `perms` to `permissions`, `plan`, `jti` to `token_id`, `sid` to `session_id`.

A request without credentials answers 401 with plain `WWW-Authenticate: Bearer` (RFC 6750).

## JWKS cache

`JwksCache` mirrors PyJWT's `PyJWKClient` but is async and rate limited:

- the key set is reused for `lifespan` (300 s);
- an unknown `kid` triggers a refresh, so a rotated key verifies at once;
- fetches are at least `refresh_cooldown` (30 s) apart, so tokens with random `kid` values cannot make the service hammer Identity;
- when a refresh fails, the last good set is used for at most `stale_max` (1 h) after the last success, then dropped;
- concurrent callers share one fetch.

`StaticKeySource` holds a fixed set (tests, or keys delivered another way); Identity uses its own key manager as `KeySource`.

## Issuing (Identity)

```python
from datetime import timedelta
from mowdeck_auth import SigningKey, TokenIssuer

key = SigningKey.generate()  # kid = RFC 7638 thumbprint
issuer = TokenIssuer(lambda: key, issuer="urn:mowdeck:identity", ttl=timedelta(minutes=10))
issued = issuer.issue(principal)  # IssuedToken(token, token_id, issued_at, expires_at, kid)
key.public_jwk()  # {"kty": "OKP", "crv": "Ed25519", "x": ..., "kid": ..., "alg": "EdDSA", "use": "sig"}
```

## Testing

```python
from mowdeck_auth.testing import TestKeys, bearer

keys = TestKeys()
app = create_app(settings, descriptor, routers=[router], principal_resolver=keys.resolver())
response = await client.get("/api/v1/x", headers=bearer(keys.mint(principal)))
keys.rotate()  # new signing key, the old one stays published
keys.server.fail = True  # the fake JWKS endpoint answers 503
```

The fake JWKS endpoint is an `httpx.MockTransport`; `keys.server.requests` counts fetches.

```bash
uv run pytest libs/mowdeck-auth --cov=mowdeck_auth
```

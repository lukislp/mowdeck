# ADR 0002: Self-built Identity service in FastAPI instead of Keycloak or Zitadel

## Status

Accepted (2026-10-08)

## Context

The platform needs accounts with owners and sub-users, login by username and password, API tokens with fine-grained permissions, and later further login methods. Households run it on a Raspberry Pi 5 with 4 GB. Off-the-shelf identity servers are feature-rich but heavy and shaped around e-mail based users.

## Decision

Build a small Identity service in FastAPI. Reasons: (1) sub-users can exist without an e-mail address (for example staff of a business); (2) API tokens carry only the permissions chosen for them (for example one token for Home Assistant); (3) login methods are plug-ins behind `CredentialAuthenticator` and `RedirectAuthenticator`, so GitHub, OIDC or passkeys are new classes; (4) services describe themselves (rights, plan features) and Identity reads these descriptions at startup; (5) the footprint stays small enough for a Raspberry Pi. Passwords use Argon2id. Identity issues short-lived JWTs (EdDSA, 5 to 15 minutes) with user, account, roles and plan, published through a JWKS endpoint. Authorization decisions stay in OpenFGA (ADR 0001).

## Consequences

- Full control over the account and sub-user model and the token content.
- We own the security of the login path: rate limits, Argon2id parameters, session handling and reviews against OWASP ASVS are our responsibility.
- Federation (OIDC providers) is added as plug-ins later instead of being available on day one. Passkeys came with [ADR 0050](0050-passkeys-and-app-login-hand-off.md), GitHub with [ADR 0051](0051-sign-in-with-github.md), Apple with [ADR 0052](0052-sign-in-with-apple.md).
- Alternatives considered: Keycloak (heavy, JVM, e-mail centric), Zitadel (good fit but a large external dependency and a different tenancy model).

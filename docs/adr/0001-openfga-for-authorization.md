# ADR 0001: OpenFGA instead of self-built authorization

## Status

Accepted (2026-10-08)

## Context

Every request must be checked against a permission model with accounts, sites, groups and single mowers, where a permission granted on one level applies to everything below. Authorization is the most security-critical part of a multi-tenant platform and the main risk is data leaking between accounts.

## Decision

Use OpenFGA (CNCF project, relationship-based access control in the style of Google Zanzibar), self-hosted on PostgreSQL, as the authorization engine. The model lives as a file in the repository and is versioned and tested like code. Services ask a single question through the thin `mowdeck-authz` library: may *who* do *what* on *which object*.

## Consequences

- No custom authorization engine to build, audit and maintain.
- The hierarchy (account, site, group, mower) maps directly to OpenFGA relations.
- One more infrastructure component to run (it adds a container and a database).
- Services depend on OpenFGA availability; `mowdeck-authz` adds caching to soften this.
- Alternatives considered: hand-written role checks in each service (error-prone), Casbin or OPA (less suited to relationship hierarchies).

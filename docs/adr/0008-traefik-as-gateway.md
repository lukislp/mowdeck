# ADR 0008: Traefik as gateway

## Status

Accepted (2026-10-08)

## Context

We need a single entry point doing TLS, routing, rate limits and token checks, running in Docker Compose and in Kubernetes with the same concepts, without writing our own gateway.

## Decision

Use Traefik v3. In Compose, services are discovered through the Docker provider and routed by labels under `/api/v1/<service>`; in Kubernetes the same routing is expressed with Ingress or IngressRoute resources. The dashboard is disabled by default. Token validation is done in the services themselves via `mowdeck-auth` (public key through JWKS); the gateway adds rate limits and may add a forward-auth check.

## Consequences

- Standard software instead of custom code.
- Docker socket access is needed for discovery in Compose; production setups should use a restricted socket proxy or the file provider.
- Traefik 3.6.1 or newer is required to talk to Docker 29.
- Alternatives considered: Caddy, nginx, Envoy (all workable; Traefik fits label based discovery best).

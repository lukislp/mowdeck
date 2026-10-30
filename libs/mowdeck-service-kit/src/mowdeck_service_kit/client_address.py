"""The address of the client behind the gateway, for rate limits, throttling and audit logs.

A service is only reachable through the gateway (Traefik in Compose, the Ingress controller in
Kubernetes). The gateway removes every ``X-Forwarded-For`` an untrusted peer sends, keeps the one
a trusted proxy in front of it sends (``GATEWAY_TRUSTED_PROXIES``) and appends its own peer before
the request reaches the service. So the header a service sees ends with entries that trusted
parties wrote, and only what lies further left can come from the client.

The rule (one for every service, the same count as the gateway's rate limits):

* ``proxy_depth`` (``MOWDECK_CLIENT_IP_DEPTH``, from ``GATEWAY_CLIENT_IP_DEPTH``) is the number
  of trusted proxies in front of the gateway that set or append ``X-Forwarded-For``. The client
  is the entry ``proxy_depth + 1`` from the right: 0 is the gateway's own peer (no proxy in
  front), 1 the address the one trusted proxy put in (Caddy, nginx). A client cannot move that
  entry, whatever it sends, as long as every proxy in front sets or appends the header.
  With fewer entries than that (a request that bypassed the proxies), the leftmost one: the
  gateway wrote it from the TCP peer, because it dropped the client's own header.
* ``proxy_depth`` unset keeps the earlier rule: the leftmost entry. It is the client only when
  every proxy in front replaces the header (Caddy with ``header_up X-Forwarded-For``); behind a
  proxy that appends (nginx ``$proxy_add_x_forwarded_for``) a client can choose it. Set the
  depth on such installations.
* Without a usable header, the TCP peer.

Entries are cut to 64 characters; they are keys and log values, not parsed addresses.
"""

from starlette.requests import Request

MAX_LENGTH = 64
"""Longest address a service keeps (an IPv6 address with a zone fits)."""


def forwarded_client(
    forwarded_for: str | None, peer: str | None, *, proxy_depth: int | None
) -> str | None:
    """The client address from an ``X-Forwarded-For`` value and the TCP peer (see the module
    rule). ``None`` only without a header entry and without a peer."""
    entries = [entry.strip() for entry in (forwarded_for or "").split(",")]
    entries = [entry for entry in entries if entry]
    if not entries:
        return peer[:MAX_LENGTH] if peer else None
    if proxy_depth is None:
        return entries[0][:MAX_LENGTH]
    index = max(0, len(entries) - 1 - proxy_depth)
    return entries[index][:MAX_LENGTH]


def client_address(request: Request, *, trust_forwarded_for: bool = True) -> str | None:
    """The client address of a request: :func:`forwarded_client` with the app's
    ``settings.client_ip_depth``; only the TCP peer when ``trust_forwarded_for`` is false (a
    service that may be reached without the gateway)."""
    peer = request.client.host if request.client else None
    if not trust_forwarded_for:
        return peer
    settings = getattr(request.app.state, "settings", None)
    depth = getattr(settings, "client_ip_depth", None)
    # Several header lines count as one list in order (RFC 9110, section 5.3).
    forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
    return forwarded_client(forwarded, peer, proxy_depth=depth)

"""Camera relay tickets (services/camera-relay): the bearer token of every WHEP request.

The media server's auth callback hands the relay whatever a client sent. Properties:

* ``verify_ticket`` raises nothing but ``InvalidTicketError`` for any string, and only accepts
  a ticket this key signed;
* ``decode_claims`` (the payload behind the signature) raises nothing but
  ``InvalidTicketError``;
* a ticket signed with the fuzzed claims verifies to exactly those claims until it expires.
"""

import base64
import json
import sys
from datetime import UTC, datetime
from uuid import UUID

import atheris

with atheris.instrument_imports():
    from camera_relay.adapters import tickets
    from camera_relay.domain.tickets import InvalidTicketError, TicketClaims

from harness import Input

_SEED = bytes(range(32))
SIGNER = tickets.Ed25519TicketSigner.from_setting(base64.b64encode(_SEED).decode("ascii"))
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _check_verify(ticket: str) -> None:
    try:
        claims = SIGNER.verify(ticket, NOW)
    except InvalidTicketError:
        return
    # Only tickets of this key verify: re-signing the claims gives the same ticket.
    assert SIGNER.sign(claims) == ticket
    assert claims.expires_at > NOW


def _check_payload(payload: bytes) -> None:
    try:
        claims = tickets.decode_claims(payload)
    except InvalidTicketError:
        return
    assert isinstance(claims, TicketClaims)
    assert all(isinstance(path, str) for path in claims.paths)


def _check_round_trip(inp: Input) -> None:
    issued = inp.int_in(0, 2**33)
    claims = TicketClaims(
        account_id=UUID(int=inp.uint(16)),
        mower_id=UUID(int=inp.uint(16)),
        relay_id=inp.text(64),
        lease_id=UUID(int=inp.uint(16)),
        paths=tuple(inp.text(80) for _ in range(inp.int_in(0, 4))),
        issued_at=datetime.fromtimestamp(issued, UTC),
        expires_at=datetime.fromtimestamp(issued + inp.int_in(0, 600), UTC),
    )
    ticket = SIGNER.sign(claims)
    if len(ticket) > tickets.MAX_TICKET_LENGTH:
        return
    at = datetime.fromtimestamp(issued + inp.int_in(0, 700), UTC)
    try:
        verified = SIGNER.verify(ticket, at)
    except InvalidTicketError:
        assert at >= claims.expires_at
        return
    assert verified == claims


def test_one_input(data: bytes) -> None:
    inp = Input(data)
    match inp.uint(1) % 3:
        case 0:
            _check_verify(inp.rest().decode("utf-8", "replace"))
        case 1:
            _check_payload(inp.rest())
        case _:
            _check_round_trip(inp)


def seeds() -> list[bytes]:
    """A valid ticket, its payload and round-trip input."""
    claims = TicketClaims(
        account_id=UUID(int=1),
        mower_id=UUID(int=2),
        relay_id="r4nd0m",
        lease_id=UUID(int=3),
        paths=("r/r4nd0m/front", "r/r4nd0m/back"),
        issued_at=NOW,
        expires_at=datetime.fromtimestamp(NOW.timestamp() + 60, UTC),
    )
    ticket = SIGNER.sign(claims).encode("ascii")
    payload = tickets.encode_claims(claims)
    other = json.loads(payload)
    other["pth"] = "not-a-list"
    return [
        b"\x00" + ticket,
        b"\x01" + payload,
        b"\x01" + json.dumps(other).encode("utf-8"),
        b"\x02" + bytes(range(1, 120)),
    ]


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()

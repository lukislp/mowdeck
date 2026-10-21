"""The camera source bridge's protocol (integrations/agora-bridge, protocol version 1).

The bridge reads JSON control lines from the relay (credentials, subscriptions) and writes
length-prefixed records; both ends are parsed here from arbitrary bytes. Properties:

* ``parse_control`` raises nothing but ``BadInputError``; a parsed ``join`` respects the limits
  of docs/dev/camera-relay-bridge-protocol.md;
* ``RecordReader`` raises nothing but ``ProtocolError``, yields the same records however the
  stream is split, and every frame it decodes encodes back to the same record;
* the relay's own reader (services/camera-relay, a separate implementation of the same
  document) accepts and rejects exactly the same streams and decodes the same records.
"""

import json
import sys

import atheris

with atheris.instrument_imports():
    from agora_bridge import protocol
    from camera_relay.adapters.media import bridge_protocol as relay

from harness import Input


def _check_control(line: bytes | str) -> None:
    try:
        control = protocol.parse_control(line)
    except protocol.BadInputError:
        return
    match control:
        case protocol.Join():
            assert 0 < len(control.app_id) <= 128
            assert 0 < len(control.channel) <= 64
            assert 0 < len(control.token) <= 2048
            assert 0 <= control.uid <= protocol.UINT32_MAX
            assert control.profile in protocol.PROFILES
        case protocol.Subscribe():
            assert len(control.uids) <= 16
            assert all(0 <= uid <= protocol.UINT32_MAX for uid in control.uids)
        case protocol.UnknownOp():
            assert len(control.op) <= 32
        case protocol.Renew() | protocol.Keyframe() | protocol.Leave():
            pass
        case _:
            raise AssertionError(type(control))


def _records(chunks: list[bytes]) -> list[object] | None:
    reader = protocol.RecordReader()
    out: list[object] = []
    try:
        for chunk in chunks:
            out.extend(reader.feed(chunk))
    except protocol.ProtocolError:
        return None
    return out


def _relay_records(stream: bytes) -> list[object] | None:
    reader = relay.RecordReader()
    try:
        return list(reader.feed(stream))
    except relay.BridgeProtocolError:
        return None


def _same(bridge: object, relayed: object) -> bool:
    if isinstance(bridge, protocol.Frame):
        return isinstance(relayed, relay.BridgeFrame) and (
            bridge.uid,
            bridge.codec,
            bridge.keyframe,
            bridge.recv_mono_ns,
            bridge.capture_ts,
            bridge.data,
        ) == (
            relayed.uid,
            relayed.codec,
            relayed.keyframe,
            relayed.recv_mono_ns,
            relayed.capture_ts,
            relayed.data,
        )
    return bridge == relayed


def _check_records(stream: bytes, cut: int) -> None:
    whole = _records([stream])
    split = _records([stream[:cut], stream[cut:]])
    assert (whole is None) == (split is None)
    if whole is not None:
        assert whole == split
    # The relay reads what the bridge writes with its own implementation of the document.
    relayed = _relay_records(stream)
    assert (whole is None) == (relayed is None)
    if whole is not None and relayed is not None:
        assert len(whole) == len(relayed)
        assert all(_same(a, b) for a, b in zip(whole, relayed, strict=True))
    for record in whole or ():
        if isinstance(record, protocol.Frame):
            encoded = protocol.encode_frame(record)
            assert protocol.decode_record(encoded[4:]) == record


def test_one_input(data: bytes) -> None:
    inp = Input(data)
    mode = inp.uint(1)
    if mode & 1:
        line = inp.rest()
        _check_control(line)
        _check_control(line.decode("utf-8", "replace"))
    else:
        cut = inp.uint(2)
        stream = inp.rest()
        _check_records(stream, cut % (len(stream) + 1))


def seeds() -> list[bytes]:
    """Valid control lines (mode byte 1) and record streams (mode byte 0)."""
    lines = [
        {"op": "join", "app_id": "a" * 32, "channel": "c1", "token": "t" * 64, "uid": 7},
        {
            "op": "join",
            "app_id": "app",
            "channel": "ch",
            "token": "tok",
            "uid": 4294967295,
            "encryption": {"mode": "aes-256-gcm2", "key": "k" * 32, "salt": "s" * 44},
            "profile": "live",
        },
        {"op": "renew", "token": "t" * 64, "uid": 1},
        {"op": "subscribe", "uids": [1, 2, 3]},
        {"op": "keyframe", "uid": 2},
        {"op": "leave"},
        {"op": "future-op"},
    ]
    out = [b"\x01" + json.dumps(line).encode("utf-8") for line in lines]
    frame = protocol.Frame(
        uid=9,
        codec=protocol.CODEC_H264,
        keyframe=True,
        recv_mono_ns=123,
        capture_ts=456,
        data=b"\x00\x00\x00\x01gB",
    )
    stream = protocol.encode_frame(frame) + protocol.encode_event("joined", uid=9)
    out.append(b"\x00\x00\x05" + stream)
    out.append(b"\x00\x00\x00" + protocol.encode_event("error", code="x", message="y"))
    return out


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()

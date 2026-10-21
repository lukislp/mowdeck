# SPDX-License-Identifier: Apache-2.0
"""The bridge protocol's records and control lines (docs/dev/camera-relay-bridge-protocol.md)."""

import json
import struct

import pytest

from agora_bridge.protocol import (
    CODEC_H265,
    MAX_RECORD,
    BadInputError,
    Encryption,
    Frame,
    Join,
    Keyframe,
    Leave,
    ProtocolError,
    RecordReader,
    Renew,
    Subscribe,
    UnknownOp,
    decode_record,
    encode_event,
    encode_frame,
    parse_control,
)

TOKEN = "007eJxTOKEN-SECRET"  # noqa: S105 - a test value


def test_a_frame_record_has_the_documented_layout() -> None:
    frame = Frame(
        uid=2, codec=CODEC_H265, keyframe=True, recv_mono_ns=5, capture_ts=7, data=b"\0\0\1\x40"
    )
    record = encode_frame(frame)
    body = bytes([0x01]) + struct.pack(">IBBQQ", 2, 2, 0b11, 5, 7) + b"\0\0\1\x40"
    assert record == struct.pack(">I", len(body)) + body
    assert decode_record(record[4:]) == frame


def test_a_frame_without_capture_time_clears_the_flag() -> None:
    frame = Frame(uid=1, codec=1, keyframe=False, recv_mono_ns=9, capture_ts=None, data=b"x")
    record = encode_frame(frame)
    assert record[4 + 1 + 5] == 0  # flags
    assert decode_record(record[4:]) == frame


def test_an_event_record_is_type_two_and_json() -> None:
    record = encode_event("hello", protocol=1)
    assert record[4] == 0x02
    assert json.loads(record[5:]) == {"event": "hello", "protocol": 1}
    assert decode_record(record[4:]) == {"event": "hello", "protocol": 1}


def test_the_reader_splits_records_across_any_chunking() -> None:
    frames = [
        Frame(uid=1, codec=2, keyframe=i == 0, recv_mono_ns=i, capture_ts=i, data=bytes(i * 3))
        for i in range(5)
    ]
    stream = b"".join([encode_event("hello"), *(encode_frame(f) for f in frames)])
    for size in (1, 3, 7, len(stream)):
        reader = RecordReader()
        records = []
        for start in range(0, len(stream), size):
            records += reader.feed(stream[start : start + size])
        assert records == [{"event": "hello"}, *frames]
        assert reader.pending == 0


@pytest.mark.parametrize("length", [0, MAX_RECORD + 1])
def test_the_reader_refuses_a_length_out_of_range(length: int) -> None:
    with pytest.raises(ProtocolError, match="out of range"):
        RecordReader().feed(struct.pack(">I", length) + b"\x02{}")


@pytest.mark.parametrize("record", [b"", b"\x09abc", b"\x01short", b"\x02not json", b"\x02[1]"])
def test_malformed_records_are_protocol_errors(record: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_record(record)


def test_a_frame_beyond_the_limit_cannot_be_encoded() -> None:
    data = bytes(MAX_RECORD)
    frame = Frame(uid=1, codec=2, keyframe=True, recv_mono_ns=0, capture_ts=None, data=data)
    with pytest.raises(ProtocolError):
        encode_frame(frame)


def test_join_parses_with_encryption_and_hides_every_secret() -> None:
    line = json.dumps(
        {
            "op": "join",
            "app_id": "app-123",
            "channel": "chan-xyz",
            "token": TOKEN,
            "uid": 123456,
            "encryption": {"mode": "aes-256-gcm2", "key": "k3y-s3cret", "salt": "c2FsdA=="},
            "extra": "ignored",
        }
    )
    join = parse_control(line)
    assert join == Join(
        app_id="app-123",
        channel="chan-xyz",
        token=TOKEN,
        uid=123456,
        encryption=Encryption(mode="aes-256-gcm2", key="k3y-s3cret", salt="c2FsdA=="),
    )
    text = repr(join)
    for secret in (TOKEN, "k3y-s3cret", "c2FsdA==", "app-123", "chan-xyz"):
        assert secret not in text
    assert join.profile == "rtc"


def test_the_other_ops_parse() -> None:
    assert parse_control('{"op": "renew", "token": "t", "uid": 5}') == Renew("t", 5)
    assert parse_control('{"op": "renew", "token": "t"}') == Renew("t", None)
    assert parse_control('{"op": "subscribe", "uids": [1, 2, 3]}') == Subscribe((1, 2, 3))
    assert parse_control('{"op": "keyframe", "uid": 1}') == Keyframe(1)
    assert parse_control('{"op": "leave"}') == Leave()
    assert parse_control('{"op": "dance"}') == UnknownOp("dance")
    assert "secret-value" not in repr(Renew("secret-value", 1))


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "[]",
        '{"no": "op"}',
        '{"op": "join", "app_id": "a", "channel": "c", "uid": 1}',
        '{"op": "join", "app_id": "a", "channel": "c", "token": "t", "uid": -1}',
        '{"op": "join", "app_id": "a", "channel": "c", "token": "t", "uid": true}',
        '{"op": "join", "app_id": "a", "channel": "c", "token": "t", "uid": 1, "profile": "x"}',
        '{"op": "join", "app_id": "a", "channel": "c", "token": "t", "uid": 1, "encryption": 1}',
        '{"op": "subscribe", "uids": "1"}',
        '{"op": "subscribe", "uids": [4294967296]}',
        '{"op": "keyframe"}',
        '{"op": "renew"}',
    ],
)
def test_bad_lines_are_bad_input(line: str) -> None:
    with pytest.raises(BadInputError):
        parse_control(line)


def test_a_bad_line_never_echoes_its_secret() -> None:
    line = json.dumps({"op": "join", "app_id": "a", "channel": "c", "token": TOKEN, "uid": "x"})
    with pytest.raises(BadInputError) as caught:
        parse_control(line)
    assert TOKEN not in str(caught.value)


def test_an_overlong_line_is_bad_input() -> None:
    with pytest.raises(BadInputError, match="too long"):
        parse_control('{"op": "leave", "pad": "' + "x" * 70_000 + '"}')


def test_a_deeply_nested_control_line_is_bad_input() -> None:
    # json.loads raises RecursionError for deep nesting: still a malformed line (exit code 2),
    # not a crash of the bridge.
    from agora_bridge.protocol import BadInputError, parse_control

    with pytest.raises(BadInputError):
        parse_control("[" * 60_000)


def test_a_deeply_nested_event_record_is_a_protocol_error() -> None:
    from agora_bridge.protocol import RECORD_EVENT, ProtocolError, decode_record

    with pytest.raises(ProtocolError):
        decode_record(bytes([RECORD_EVENT]) + b"[" * 100_000)

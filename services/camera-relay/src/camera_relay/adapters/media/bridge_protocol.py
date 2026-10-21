"""The relay's side of the camera source bridge protocol, version 1.

docs/dev/camera-relay-bridge-protocol.md: the relay writes JSON control lines and reads
length-prefixed records (frames and events). The bridge (``integrations/agora-bridge``) has
its own implementation; both follow the document and are tested against its byte layout, so
the AGPL relay never imports bridge code and the bridge never imports relay code.

Over the socket transport (the bridge server next to the relay) the bytes are the same; the
server adds one event when the bridge process ends: ``{"event": "exit", "code": N}``.
"""

import json
import struct
from dataclasses import dataclass, field
from typing import Any

from mowdeck_contracts.v1 import CameraSession

MAX_RECORD = 4 * 1024 * 1024 + 64
RECORD_FRAME = 0x01
RECORD_EVENT = 0x02
CODEC_H264 = 1
CODEC_H265 = 2
FLAG_KEYFRAME = 0x01
FLAG_CAPTURE_TS = 0x02

_FRAME_HEADER = struct.Struct(">IBBQQ")
_LENGTH = struct.Struct(">I")


class BridgeProtocolError(ValueError):
    """The bridge sent something that does not follow the protocol."""


@dataclass(frozen=True, slots=True)
class BridgeFrame:
    """One access unit of one publisher (Annex-B; a key frame carries its parameter sets)."""

    uid: int
    codec: int
    keyframe: bool
    recv_mono_ns: int
    capture_ts: int | None
    data: bytes = field(repr=False)


BridgeRecord = BridgeFrame | dict[str, Any]


def decode_record(record: bytes) -> BridgeRecord:
    """A record without its length prefix."""
    if not record:
        msg = "empty record"
        raise BridgeProtocolError(msg)
    kind, body = record[0], record[1:]
    if kind == RECORD_FRAME:
        if len(body) < _FRAME_HEADER.size:
            msg = "frame record too short"
            raise BridgeProtocolError(msg)
        uid, codec, flags, recv, capture = _FRAME_HEADER.unpack_from(body)
        return BridgeFrame(
            uid=uid,
            codec=codec,
            keyframe=bool(flags & FLAG_KEYFRAME),
            recv_mono_ns=recv,
            capture_ts=capture if flags & FLAG_CAPTURE_TS else None,
            data=bytes(body[_FRAME_HEADER.size :]),
        )
    if kind == RECORD_EVENT:
        try:
            event = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            msg = "event record is not JSON"
            raise BridgeProtocolError(msg) from exc
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            msg = "event record has no event name"
            raise BridgeProtocolError(msg)
        return event
    msg = f"unknown record type {kind}"
    raise BridgeProtocolError(msg)


def encode_frame(frame: BridgeFrame) -> bytes:
    """A frame record with its length prefix (the tests' fake bridge)."""
    flags = (FLAG_KEYFRAME if frame.keyframe else 0) | (
        FLAG_CAPTURE_TS if frame.capture_ts is not None else 0
    )
    header = _FRAME_HEADER.pack(
        frame.uid, frame.codec, flags, frame.recv_mono_ns, frame.capture_ts or 0
    )
    return (
        _LENGTH.pack(1 + len(header) + len(frame.data))
        + bytes([RECORD_FRAME])
        + header
        + (frame.data)
    )


def encode_event(event: str, **fields: Any) -> bytes:
    """An event record with its length prefix (the tests' fake bridge)."""
    body = json.dumps({"event": event, **fields}, separators=(",", ":")).encode("utf-8")
    return _LENGTH.pack(1 + len(body)) + bytes([RECORD_EVENT]) + body


class RecordReader:
    """Splits the bridge's byte stream into records."""

    def __init__(self, max_record: int = MAX_RECORD) -> None:
        self._buffer = bytearray()
        self._max = max_record

    def feed(self, data: bytes) -> list[BridgeRecord]:
        """Add ``data``; every complete record. A length beyond the limit is a protocol
        error (the relay stops the bridge)."""
        self._buffer += data
        records: list[BridgeRecord] = []
        while len(self._buffer) >= _LENGTH.size:
            (size,) = _LENGTH.unpack_from(self._buffer)
            if size == 0 or size > self._max:
                msg = f"record length {size} out of range"
                raise BridgeProtocolError(msg)
            if len(self._buffer) < _LENGTH.size + size:
                break
            record = bytes(self._buffer[_LENGTH.size : _LENGTH.size + size])
            del self._buffer[: _LENGTH.size + size]
            records.append(decode_record(record))
        return records


def control_line(op: str, **fields: Any) -> bytes:
    """One control line (may carry secrets: never log it)."""
    return json.dumps({"op": op, **fields}, separators=(",", ":")).encode("utf-8") + b"\n"


def join_line(session: CameraSession) -> bytes:
    """``join`` with the vendor session's credentials (secret)."""
    encryption = None
    if session.encryption is not None:
        encryption = {
            "mode": session.encryption.mode,
            "key": session.encryption.key,
            "salt": session.encryption.salt,
        }
    return control_line(
        "join",
        app_id=session.app_id,
        channel=session.channel,
        token=session.token,
        uid=session.uid,
        encryption=encryption,
    )


def renew_line(session: CameraSession) -> bytes:
    """``renew`` with a fresh session's token (secret)."""
    return control_line("renew", token=session.token, uid=session.uid)

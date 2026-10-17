# SPDX-License-Identifier: Apache-2.0
"""The camera source bridge protocol, version 1 (docs/dev/camera-relay-bridge-protocol.md).

* stdin: control lines, one JSON object per line (:func:`parse_control`);
* stdout: records, a 4-byte big-endian length ``L`` and ``L`` bytes: one type byte and the body
  (:func:`encode_frame`, :func:`encode_event`, :class:`RecordReader`).

Secrets (token, encryption key and salt) arrive only on stdin and are kept out of every
``repr``; events never carry them.
"""

import json
import struct
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1
MAX_RECORD = 4 * 1024 * 1024 + 64
"""Largest record (type byte and body) a reader accepts."""
MAX_LINE = 64 * 1024
"""Largest control line the bridge accepts."""

RECORD_FRAME = 0x01
RECORD_EVENT = 0x02

CODEC_H264 = 1
CODEC_H265 = 2

FLAG_KEYFRAME = 0x01
FLAG_CAPTURE_TS = 0x02

_FRAME_HEADER = struct.Struct(">IBBQQ")
"""uid, codec, flags, recv_mono_ns, capture_ts: 22 bytes."""
_LENGTH = struct.Struct(">I")

UINT32_MAX = 2**32 - 1
PROFILES = ("rtc", "live")


class ProtocolError(ValueError):
    """A record that does not follow the protocol."""


class BadInputError(ValueError):
    """A control line that does not follow the protocol (exit code 2)."""


@dataclass(frozen=True, slots=True)
class Frame:
    """One encoded access unit of one publisher."""

    uid: int
    codec: int
    keyframe: bool
    recv_mono_ns: int
    capture_ts: int | None
    data: bytes = field(repr=False)


def encode_frame(frame: Frame) -> bytes:
    """The frame record (length prefix included)."""
    flags = (FLAG_KEYFRAME if frame.keyframe else 0) | (
        FLAG_CAPTURE_TS if frame.capture_ts is not None else 0
    )
    header = _FRAME_HEADER.pack(
        frame.uid, frame.codec, flags, frame.recv_mono_ns, frame.capture_ts or 0
    )
    size = 1 + len(header) + len(frame.data)
    if size > MAX_RECORD:
        msg = f"frame of {len(frame.data)} bytes exceeds the record limit"
        raise ProtocolError(msg)
    return _LENGTH.pack(size) + bytes([RECORD_FRAME]) + header + frame.data


def encode_event(event: str, **fields: Any) -> bytes:
    """The event record ``{"event": event, **fields}`` (length prefix included)."""
    body = json.dumps({"event": event, **fields}, separators=(",", ":")).encode("utf-8")
    return _LENGTH.pack(1 + len(body)) + bytes([RECORD_EVENT]) + body


def decode_record(record: bytes) -> Frame | dict[str, Any]:
    """A record without its length prefix: a :class:`Frame` or an event object."""
    if not record:
        msg = "empty record"
        raise ProtocolError(msg)
    kind, body = record[0], record[1:]
    if kind == RECORD_FRAME:
        if len(body) < _FRAME_HEADER.size:
            msg = "frame record too short"
            raise ProtocolError(msg)
        uid, codec, flags, recv, capture = _FRAME_HEADER.unpack_from(body)
        return Frame(
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
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            msg = "event record is not JSON"
            raise ProtocolError(msg) from exc
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            msg = "event record has no event name"
            raise ProtocolError(msg)
        return event
    msg = f"unknown record type {kind}"
    raise ProtocolError(msg)


class RecordReader:
    """Splits a byte stream into records (incrementally)."""

    def __init__(self, max_record: int = MAX_RECORD) -> None:
        self._buffer = bytearray()
        self._max = max_record

    def feed(self, data: bytes) -> list[Frame | dict[str, Any]]:
        """Add ``data`` and return every complete record."""
        self._buffer += data
        records: list[Frame | dict[str, Any]] = []
        while len(self._buffer) >= _LENGTH.size:
            (size,) = _LENGTH.unpack_from(self._buffer)
            if size == 0 or size > self._max:
                msg = f"record length {size} out of range"
                raise ProtocolError(msg)
            if len(self._buffer) < _LENGTH.size + size:
                break
            record = bytes(self._buffer[_LENGTH.size : _LENGTH.size + size])
            del self._buffer[: _LENGTH.size + size]
            records.append(decode_record(record))
        return records

    @property
    def pending(self) -> int:
        """Bytes of an incomplete record."""
        return len(self._buffer)


# --- Control lines ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Encryption:
    """Channel encryption (``mode`` as the vendor names it, e.g. ``aes-256-gcm2``)."""

    mode: str
    key: str = field(repr=False)
    salt: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class Join:
    """``join``: the session's credentials."""

    app_id: str = field(repr=False)
    channel: str = field(repr=False)
    token: str = field(repr=False)
    uid: int
    encryption: Encryption | None = None
    profile: str = "rtc"


@dataclass(frozen=True, slots=True)
class Renew:
    """``renew``: a fresh token for the running connection."""

    token: str = field(repr=False)
    uid: int | None = None


@dataclass(frozen=True, slots=True)
class Subscribe:
    """``subscribe``: publishers whose encoded video to receive."""

    uids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Keyframe:
    """``keyframe``: ask a publisher for a key frame (best effort)."""

    uid: int


@dataclass(frozen=True, slots=True)
class Leave:
    """``leave``: leave the channel and exit."""


@dataclass(frozen=True, slots=True)
class UnknownOp:
    """A line with an ``op`` this version does not know (answered with an ``error`` event)."""

    op: str


Control = Join | Renew | Subscribe | Keyframe | Leave | UnknownOp


def _text(data: dict[str, Any], name: str, *, max_length: int, required: bool = True) -> str:
    value = data.get(name)
    if not isinstance(value, str) or (required and not value) or len(value) > max_length:
        msg = f"{name} must be a string of at most {max_length} characters"
        raise BadInputError(msg)
    return value


def _uid(value: Any, name: str = "uid") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= UINT32_MAX:
        msg = f"{name} must be an unsigned 32-bit integer"
        raise BadInputError(msg)
    return value


def _encryption(value: Any) -> Encryption | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        msg = "encryption must be an object or null"
        raise BadInputError(msg)
    salt = value.get("salt")
    if salt is not None and (not isinstance(salt, str) or len(salt) > 256):
        msg = "encryption.salt must be a string or null"
        raise BadInputError(msg)
    return Encryption(
        mode=_text(value, "mode", max_length=32),
        key=_text(value, "key", max_length=256),
        salt=salt or None,
    )


def parse_control(line: str | bytes) -> Control:
    """One control line; raises :class:`BadInputError` (never with a secret in the message)."""
    if len(line) > MAX_LINE:
        msg = "control line too long"
        raise BadInputError(msg)
    try:
        data = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = "control line is not JSON"
        raise BadInputError(msg) from exc
    if not isinstance(data, dict) or not isinstance(data.get("op"), str):
        msg = "control line has no op"
        raise BadInputError(msg)
    op = data["op"]
    match op:
        case "join":
            profile = data.get("profile") or "rtc"
            if profile not in PROFILES:
                msg = f"profile must be one of {', '.join(PROFILES)}"
                raise BadInputError(msg)
            return Join(
                app_id=_text(data, "app_id", max_length=128),
                channel=_text(data, "channel", max_length=64),
                token=_text(data, "token", max_length=2048),
                uid=_uid(data.get("uid")),
                encryption=_encryption(data.get("encryption")),
                profile=profile,
            )
        case "renew":
            uid = data.get("uid")
            return Renew(
                token=_text(data, "token", max_length=2048),
                uid=None if uid is None else _uid(uid),
            )
        case "subscribe":
            uids = data.get("uids")
            if not isinstance(uids, list) or len(uids) > 16:
                msg = "uids must be a list of at most 16 uids"
                raise BadInputError(msg)
            return Subscribe(tuple(_uid(uid, "uids[]") for uid in uids))
        case "keyframe":
            return Keyframe(_uid(data.get("uid")))
        case "leave":
            return Leave()
        case _:
            return UnknownOp(op[:32])

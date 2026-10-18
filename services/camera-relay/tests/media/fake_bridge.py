"""A stand-in for agora-bridge in the media test stack: the spike's recorded stream through the
bridge protocol (docs/dev/camera-relay-bridge-protocol.md), standard library only.

The agora-bridge server (``integrations/agora-bridge``, run from source in the test stack)
starts this script once per relay connection instead of the real bridge, so the relay's agora
driver, muxer and transcoder run exactly as with the mower, without Agora:

* ``join`` -> ``joined`` and the publishers 1, 2 and 3 (3, the rear camera, never sends);
* ``subscribe`` -> the recorded access units of ``luba-uid1.h265`` (H.265, 640x480, 10 fps,
  4 s GOP) for uid 1 and 2, looped, paced at 10 fps, with ``recv_mono_ns`` of this process;
* a channel that starts with ``crash`` exits with code 1 after 150 frames (a crashing SDK);
* a channel that starts with ``late`` starts each camera mid-GOP (its first key frame comes
  after about 4 s, like the LUBA's) and then sends nothing for ``LATE_HOLD`` seconds after
  that key frame (a 4G stall): the muxer cannot finish its probe, ``.../src`` appears late;
* a channel that starts with ``stall`` replays the live incident of 2026-10-18 (the LUBA's 4G
  link while it mows): both cameras play for ``STALL_AFTER`` seconds, then uid 2 falls silent,
  is reported ``unpublished`` after ``FLAP_HOLD`` seconds and never comes back; uid 1 flaps
  ``FLAPS`` times (``FLAP_HOLD`` seconds silent, longer than MediaMTX's read timeout of the
  time, then ``FLAP_PLAY`` seconds of frames, mid-GOP), falls silent once more, is reported
  ``unpublished`` and ``REPUBLISH_GAP`` seconds later ``published`` again, and then plays on;
* ``leave`` or the end of stdin -> ``left``, exit 0. ``--check`` exits 0.
"""

import json
import os
import struct
import sys
import threading
import time
from pathlib import Path

LOCK = threading.Lock()
OUT = sys.stdout.buffer
STOP = threading.Event()
CRASH_AFTER = 150
"""Frames before a "crash" channel ends the bridge (15 s: the viewer is connected by then)."""
LATE_HOLD = 6.0
"""Seconds a "late" channel stays silent after its first key frame."""
STALL_AFTER = 8.0
"""Seconds a "stall" channel plays before its link stalls."""
FLAP_HOLD = 12.0
"""Seconds of silence of every stall of a "stall" channel."""
FLAP_PLAY = 5.0
"""Seconds of frames between two stalls (at least one key frame of the 4 s GOP)."""
FLAPS = 3
"""Stalls of uid 1 before its publisher leaves and comes back."""
REPUBLISH_GAP = 2.0
"""Seconds between ``unpublished`` and ``published`` of uid 1."""


class Stall:
    """The schedule of one camera of a "stall" channel: when to fall silent, for how long, and
    which publisher events to send afterwards."""

    def __init__(self, uid: int) -> None:
        self.uid = uid
        self.next_at = time.monotonic() + STALL_AFTER
        self.flaps = 0
        self.done = False

    def due(self) -> bool:
        return not self.done and time.monotonic() >= self.next_at

    def run(self) -> bool:
        """Sleep through the stall; False when this camera sends nothing any more."""
        time.sleep(FLAP_HOLD)
        if self.uid != 1:
            event("publisher", uid=self.uid, state="unpublished")
            self.done = True
            return False
        self.flaps += 1
        if self.flaps <= FLAPS:
            self.next_at = time.monotonic() + FLAP_PLAY
            return True
        event("publisher", uid=self.uid, state="unpublished")
        time.sleep(REPUBLISH_GAP)
        event("publisher", uid=self.uid, state="published")
        self.done = True
        return True


def record(kind: int, body: bytes) -> None:
    with LOCK:
        OUT.write(struct.pack(">I", 1 + len(body)) + bytes([kind]) + body)
        OUT.flush()


def event(name: str, **fields: object) -> None:
    record(0x02, json.dumps({"event": name, **fields}).encode())


def frame(uid: int, keyframe: bool, data: bytes) -> None:
    header = struct.pack(">IBBQQ", uid, 2, 1 if keyframe else 0, time.monotonic_ns(), 0)
    record(0x01, header + data)


def nal_units(data: bytes) -> list[bytes]:
    starts: list[int] = []
    i = 0
    while i < len(data) - 3:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            starts.append(i + 3)
            i += 3
        else:
            i += 1
    units = []
    for k, start in enumerate(starts):
        end = starts[k + 1] - 3 if k + 1 < len(starts) else len(data)
        while k + 1 < len(starts) and end > start and data[end - 1] == 0:
            end -= 1
        units.append(data[start:end])
    return units


def access_units(data: bytes) -> list[tuple[bool, bytes]]:
    """H.265 Annex-B split into access units (key: an IRAP picture)."""
    units: list[list[bytes]] = []
    current: list[bytes] = []
    has_picture = False
    for nal in nal_units(data):
        kind = (nal[0] >> 1) & 0x3F
        first_slice = kind < 32 and len(nal) > 2 and bool(nal[2] & 0x80)
        if has_picture and (kind in (32, 33, 34, 35, 39) or first_slice):
            units.append(current)
            current, has_picture = [], False
        current.append(nal)
        has_picture = has_picture or kind < 32
    if current:
        units.append(current)
    return [
        (
            any(16 <= ((n[0] >> 1) & 0x3F) <= 23 for n in unit),
            b"".join(b"\x00\x00\x00\x01" + n for n in unit),
        )
        for unit in units
    ]


def replay(
    uid: int, units: list[tuple[bool, bytes]], crash: bool, late: bool, stall: bool = False
) -> None:
    sent = 0
    next_at = time.monotonic()
    schedule = Stall(uid) if stall else None
    if late:  # start right after the first key frame: the next one is a GOP away
        first_key = next(i for i, (keyframe, _) in enumerate(units) if keyframe)
        units = units[first_key + 1 :] + units[: first_key + 1]
    held = not late
    while not STOP.is_set():
        for keyframe, data in units:
            if STOP.is_set():
                return
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            next_at += 0.1
            frame(uid, keyframe, data)
            sent += 1
            if keyframe and not held:
                held = True
                time.sleep(LATE_HOLD)
                next_at = time.monotonic() + 0.1
            if schedule is not None and schedule.due():
                if not schedule.run():
                    return
                next_at = time.monotonic() + 0.1
            if crash and sent >= CRASH_AFTER:
                event("error", code="connection_failed", detail="simulated crash")
                OUT.flush()
                os._exit(1)


def main() -> int:
    if "--check" in sys.argv:
        return 0
    units = access_units(Path(sys.argv[1]).read_bytes())
    event("hello", protocol=1, bridge="fake-bridge/1", sdk="none (recorded stream)")
    crash = late = stall = False
    started: set[int] = set()
    for line in sys.stdin:
        control = json.loads(line)
        op = control.get("op")
        if op == "join":
            crash = str(control.get("channel", "")).startswith("crash")
            late = str(control.get("channel", "")).startswith("late")
            stall = str(control.get("channel", "")).startswith("stall")
            event("joined", elapsed_ms=5)
            for uid in (1, 2, 3):
                event("publisher", uid=uid, state="published")
        elif op == "subscribe":
            for uid in control.get("uids", []):
                if uid in (1, 2) and uid not in started:
                    started.add(uid)
                    threading.Thread(
                        target=replay, args=(uid, units, crash, late, stall), daemon=True
                    ).start()
        elif op == "renew":
            event("renewed")
        elif op == "leave":
            break
    STOP.set()
    event("left")
    return 0


if __name__ == "__main__":
    sys.exit(main())

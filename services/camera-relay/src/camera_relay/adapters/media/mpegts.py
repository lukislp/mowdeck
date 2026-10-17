"""A minimal MPEG transport stream writer for one H.265 or H.264 video stream.

The bridge source hands each camera's access units to an ffmpeg that publishes them to
``r/<rid>/<cam>/src`` (``-f mpegts -i pipe:0 -c copy -f rtsp``). MPEG-TS carries the
presentation time per frame, so the relay decides the timestamps (from the bridge's receive
time) instead of letting ffmpeg guess them from a raw elementary stream.

Latency: ffmpeg's demuxer runs a parser that finds the end of an access unit only at the start
of the next one, which would hold every frame back by one frame interval (100 ms at 10 fps).
Each PES packet therefore ends with an access unit delimiter (AUD), the start of the next access
unit: the parser emits the frame as soon as its PES packet is complete. The parser gives a frame
the timestamp of the PES packet in which the frame *starts*, i.e. the one carrying the
delimiter, so every frame gets the timestamp of the frame before: :meth:`TsWriter.start` writes
a lone delimiter one nominal frame interval before the first frame, and the stream is shifted by
one frame with the true intervals between frames (verified with ffmpeg 7.1: no duplicated or
unset timestamp, no added delay).

Packets: PAT (PID 0) and PMT (PID 0x1000) before every key frame, video on PID 0x100 with the
PCR in the first packet of every frame and ``random_access_indicator`` on key frames.
"""

from camera_relay.adapters.media.bridge_protocol import CODEC_H264, CODEC_H265

PAT_PID = 0x0000
PMT_PID = 0x1000
VIDEO_PID = 0x0100
PROGRAM = 1
CLOCK = 90_000
"""The 90 kHz clock of PTS and PCR."""
PACKET = 188
_PAYLOAD = 184
_PTS_WRAP = 1 << 33

STREAM_TYPES = {CODEC_H264: 0x1B, CODEC_H265: 0x24}
AUD = {
    CODEC_H265: b"\x00\x00\x00\x01\x46\x01\x50",
    CODEC_H264: b"\x00\x00\x00\x01\x09\xf0",
}
"""An access unit delimiter per codec (H.265 type 35, H.264 type 9; any picture type)."""


def _crc_table() -> list[int]:
    table = []
    for byte in range(256):
        crc = byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
        table.append(crc & 0xFFFFFFFF)
    return table


_CRC_TABLE = _crc_table()


def crc32_mpeg2(data: bytes) -> int:
    """The CRC of PSI sections (CRC-32/MPEG-2)."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) ^ byte) & 0xFF]
    return crc


def _pts_bytes(pts: int) -> bytes:
    pts %= _PTS_WRAP
    return bytes(
        [
            0x21 | ((pts >> 29) & 0x0E),
            (pts >> 22) & 0xFF,
            ((pts >> 14) & 0xFE) | 1,
            (pts >> 7) & 0xFF,
            ((pts << 1) & 0xFE) | 1,
        ]
    )


def _pcr_bytes(pcr: int) -> bytes:
    base = pcr % _PTS_WRAP
    return bytes(
        [
            (base >> 25) & 0xFF,
            (base >> 17) & 0xFF,
            (base >> 9) & 0xFF,
            (base >> 1) & 0xFF,
            ((base & 1) << 7) | 0x7E,
            0x00,
        ]
    )


class TsWriter:
    """Writes one program with one video stream; one instance per ffmpeg process."""

    def __init__(self, codec: int) -> None:
        if codec not in STREAM_TYPES:
            msg = f"codec {codec} is not H.264 or H.265"
            raise ValueError(msg)
        self.codec = codec
        self._continuity: dict[int, int] = {}

    def start(self, first_pts: int, interval: int = CLOCK // 10) -> bytes:
        """PSI and the lone delimiter that starts the first frame, one ``interval`` before
        ``first_pts`` (see the module docstring)."""
        return self.psi() + self._pes(b"", first_pts - interval, keyframe=False)

    def psi(self) -> bytes:
        """PAT and PMT."""
        pat = bytes([0x00, 0xB0, 0x0D, 0x00, PROGRAM, 0xC1, 0x00, 0x00])
        pat += bytes([0x00, PROGRAM, 0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF])
        pat += crc32_mpeg2(pat).to_bytes(4, "big")
        stream = bytes(
            [STREAM_TYPES[self.codec], 0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0x00]
        )
        body = bytes([0x00, PROGRAM, 0xC1, 0x00, 0x00])
        body += bytes([0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0x00]) + stream
        length = len(body) + 4
        pmt = bytes([0x02, 0xB0 | (length >> 8), length & 0xFF]) + body
        pmt += crc32_mpeg2(pmt).to_bytes(4, "big")
        return self._packets(PAT_PID, b"\x00" + pat, unit_start=True) + self._packets(
            PMT_PID, b"\x00" + pmt, unit_start=True
        )

    def frame(self, access_unit: bytes, pts: int, *, keyframe: bool) -> bytes:
        """One access unit (Annex-B) at ``pts`` (90 kHz), PSI first on a key frame."""
        head = self.psi() if keyframe else b""
        return head + self._pes(access_unit, pts, keyframe=keyframe)

    def _pes(self, access_unit: bytes, pts: int, *, keyframe: bool) -> bytes:
        header = bytes([0x80, 0x80, 0x05]) + _pts_bytes(pts)
        payload = header + access_unit + AUD[self.codec]
        length = len(payload) if len(payload) <= 0xFFFF else 0
        pes = b"\x00\x00\x01\xe0" + length.to_bytes(2, "big") + payload
        return self._packets(VIDEO_PID, pes, unit_start=True, pcr=pts, keyframe=keyframe)

    def _packets(
        self,
        pid: int,
        payload: bytes,
        *,
        unit_start: bool,
        pcr: int | None = None,
        keyframe: bool = False,
    ) -> bytes:
        out = bytearray()
        first = True
        while first or payload:
            continuity = self._continuity.get(pid, 0)
            self._continuity[pid] = (continuity + 1) & 0x0F
            header = bytes(
                [0x47, (0x40 if unit_start and first else 0x00) | (pid >> 8), pid & 0xFF]
            )
            adaptation = b""
            if first and pcr is not None:
                flags = 0x10 | (0x40 if keyframe else 0x00)
                adaptation = bytes([flags]) + _pcr_bytes(pcr)
            room = _PAYLOAD - (len(adaptation) + 1 if adaptation else 0)
            chunk, payload = payload[:room], payload[room:]
            stuffing = room - len(chunk)
            if not adaptation and stuffing == 0:
                out += header + bytes([0x10 | continuity]) + chunk
            else:
                if not adaptation:
                    # An adaptation field of its own: its length byte takes one byte of room.
                    adaptation = b"" if stuffing == 1 else b"\x00" + b"\xff" * (stuffing - 2)
                else:
                    adaptation += b"\xff" * stuffing
                out += header + bytes([0x30 | continuity, len(adaptation)]) + adaptation + chunk
            first = False
        return bytes(out)

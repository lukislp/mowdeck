# SPDX-License-Identifier: Apache-2.0
"""Summarise a spike run: per uid frame count, fps, key frame interval (GOP), frame sizes,
capture timestamps and a latency estimate (receive wall clock minus capture time, only
meaningful when the mower's capture clock is epoch milliseconds and roughly NTP synced).

Usage: ``python analyze.py <out-dir>`` (reads ``frames.csv`` and ``events.jsonl``).
"""

import csv
import json
import statistics
import sys
from collections import defaultdict
from itertools import pairwise
from pathlib import Path

KEY = 3


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def main(out: Path) -> None:
    rows = defaultdict(list)
    with (out / "frames.csv").open() as f:
        for r in csv.DictReader(f):
            rows[int(r["uid"])].append(r)
    summary = {}
    for uid, frames in sorted(rows.items()):
        t = [float(r["t"]) for r in frames]
        sizes = [int(r["bytes"]) for r in frames]
        keys = [i for i, r in enumerate(frames) if int(r["frame_type"]) == KEY]
        gops_frames = [b - a for a, b in pairwise(keys)]
        gops_s = [t[b] - t[a] for a, b in pairwise(keys)]
        span = t[-1] - t[0] if len(t) > 1 else 0
        cap = [int(r["capture_time_ms"]) for r in frames]
        wall = [int(r["wall_ms"]) for r in frames]
        lat = [w - c for w, c in zip(wall, cap, strict=False) if c > 1_000_000_000_000]
        cap_steps = [b - a for a, b in pairwise(cap) if a and b]
        intervals = [b - a for a, b in pairwise(t)]
        summary[uid] = {
            "frames": len(frames),
            "seconds": round(span, 1),
            "fps": round((len(frames) - 1) / span, 2) if span else None,
            "codecs": sorted({r["codec"] for r in frames}),
            "frame_types": sorted({r["frame_type"] for r in frames}),
            "resolution": sorted({f"{r['width']}x{r['height']}" for r in frames}),
            "annexb": sorted({r["annexb"] for r in frames}),
            "key_frames": len(keys),
            "key_nal_types": sorted({frames[i]["nal_types"] for i in keys}),
            "delta_nal_types": sorted(
                {r["nal_types"] for r in frames if int(r["frame_type"]) != KEY}
            )[:5],
            "gop_frames_median": statistics.median(gops_frames) if gops_frames else None,
            "gop_seconds_median": round(statistics.median(gops_s), 2) if gops_s else None,
            "gop_seconds_minmax": [round(min(gops_s), 2), round(max(gops_s), 2)]
            if gops_s
            else None,
            "bytes_key_median": statistics.median([sizes[i] for i in keys]) if keys else None,
            "bytes_delta_median": statistics.median(
                [s for i, s in enumerate(sizes) if i not in set(keys)]
            )
            if sizes
            else None,
            "kbit_s": round(sum(sizes) * 8 / span / 1000, 1) if span else None,
            "capture_time_present": any(cap),
            "capture_step_ms_median": statistics.median(cap_steps) if cap_steps else None,
            "latency_ms_p50": pct(lat, 0.5) if lat else None,
            "latency_ms_p95": pct(lat, 0.95) if lat else None,
            "recv_gap_ms_p95": round(pct(intervals, 0.95) * 1000) if intervals else None,
            "recv_gap_ms_max": round(max(intervals) * 1000) if intervals else None,
        }
    events = [json.loads(line) for line in (out / "events.jsonl").open()]
    intra = next((e for e in events if e["event"] == "intra_request"), None)
    if intra:
        uid = int(intra["uid"])
        after = [
            float(r["t"])
            for r in rows.get(uid, [])
            if int(r["frame_type"]) == KEY and float(r["t"]) > intra["t"]
        ]
        summary["intra_request"] = {
            "ret": intra["ret"],
            "sent_at": intra["t"],
            "next_key_after_s": round(after[0] - intra["t"], 2) if after else None,
        }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]))

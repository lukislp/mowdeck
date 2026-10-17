#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Step 0 spike run on the host of the stack (throwaway container, host network to reach the
# stack's NATS on 127.0.0.1:4222). Does not touch the Compose project.
#
#   MOWER_ID=<uuid> ACCOUNT_ID=<uuid> ./run_spike.sh [out-dir]
#
# Two sessions are requested: the first to join, the second (60 s later) to renew the token.
# Both stay inside the pipe; nothing secret is written to disk.
set -eu
: "${MOWER_ID:?set MOWER_ID}"
: "${ACCOUNT_ID:?set ACCOUNT_ID}"
OUT="${1:-$PWD/out}"
DURATION="${DURATION:-90}"
REJOIN_AFTER="${REJOIN_AFTER:-120}"
export MOWER_ID ACCOUNT_ID DURATION REJOIN_AFTER
mkdir -p "$OUT"
HERE="$(cd "$(dirname "$0")" && pwd)"
docker build -q -t relay-spike:tmp "$HERE" >/dev/null

docker run --rm -i --network host -v "$OUT:/out" -e MOWER_ID -e ACCOUNT_ID \
  -e DURATION -e REJOIN_AFTER relay-spike:tmp sh -c '
    {
      python /code/get_session.py --mower "$MOWER_ID" --account "$ACCOUNT_ID"
      sleep 60
      python /code/get_session.py --mower "$MOWER_ID" --account "$ACCOUNT_ID"
    } | python /code/bridge_spike.py --out /out --duration "$DURATION" --renew \
          --rejoin-after "$REJOIN_AFTER" --rejoin-duration 20
    python /code/analyze.py /out > /out/summary.json
    ffprobe -v error -show_streams -count_frames -of json /out/luba-uid1.h265 > /out/ffprobe.json
    ffmpeg -v error -y -i /out/luba-uid1.h265 -c:v libx264 -profile:v baseline \
      -preset veryfast -tune zerolatency -fps_mode passthrough -an /out/luba-uid1.mp4
    ffprobe -v error -show_streams -of json /out/luba-uid1.mp4 > /out/ffprobe-h264.json
  '
echo "results in $OUT"

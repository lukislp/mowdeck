#!/usr/bin/env bash
# Load test of the real public demo host, level by level (docs/ops/load-testing.md, "Measuring
# the real demo host"). Run it ON the demo host, as its operator, in an announced window, with
# tests/load/vps/sample.sh running in a second shell:
#
#   tests/load/vps/ladder.sh [level ...]        default levels: 10 20 30 40
#
# k6 runs in the official grafana/k6 image inside the network namespace of the demo's TLS proxy
# (docker run --network container:<proxy>). Its requests therefore reach the MowDeck gateway from
# the proxy's own address, the only one in GATEWAY_TRUSTED_PROXIES, and the gateway counts every
# virtual visitor by the X-Forwarded-For that k6 sends (198.18.x.y, the benchmark range), exactly
# as it counts a real visitor whose address Caddy writes there. Nothing of the demo's or the
# proxy's configuration changes; Cloudflare and Caddy themselves are not part of the measurement.
#
# Each level runs the scenario sandbox-visitor with LOAD_VUS=<level>: that many visitors arrive
# within LOAD_RAMP, each claims a sandbox, starts a mower and watches it for LOAD_DURATION, then
# logs out. After a level the script waits LOAD_PAUSE seconds so that the demo deletes those
# sandboxes, and prints the sandbox counts. A level that crosses k6's thresholds (API p95 500 ms,
# 1 % failed requests, 99 % of the live views ready and with states) exits 99; the ladder stops
#
# - after a level with LOAD_STOP_FAILED_PCT (1) % failed requests or more, fewer than 99 % of the
#   live views ready, or an API p95 of LOAD_STOP_P95_MS (2000) or more, and
# - at once, through a watchdog that stops k6 with SIGINT (k6 still writes its summary), when swap
#   in use grows by LOAD_MAX_SWAP_GROWTH_MIB, the 1-minute load average passes LOAD_MAX_LOAD1,
#   or one of the other demos (LOAD_PROBES) fails two probes in a row.
#
# Results: LOAD_RESULTS (default ./load-results/<UTC time>), one directory per level (k6.txt,
# summary.json, row.csv) and levels.csv with one row per level.
set -euo pipefail

proxy="${LOAD_PROXY_CONTAINER:-lktec-web}"
base_url="${LOAD_BASE_URL:-http://mowdeck-demo-gateway}"
origin="${LOAD_ORIGIN:-https://mowdeck-demo.lktec.org}"
project="${LOAD_DEMO_PROJECT:-mowdeck-demo}"
lock="${LOAD_DEMO_LOCK-/run/lock/mowdeck-demo.lock}"
k6_image="${K6_IMAGE:-grafana/k6:1.3.0@sha256:3ddc8b1a33a2c3d8edc6e99b6a762ae36cba08788463458f5e6a7703e14eb77d}"
k6_cpus="${LOAD_K6_CPUS:-0.5}"
k6_memory="${LOAD_K6_MEMORY:-1g}"
ramp="${LOAD_RAMP:-60s}"
duration="${LOAD_DURATION:-5m}"
hold="${LOAD_HOLD:-120}"
pause="${LOAD_PAUSE:-120}"
stop_p95="${LOAD_STOP_P95_MS:-2000}"
stop_failed="${LOAD_STOP_FAILED_PCT:-1}"
max_swap_growth="${LOAD_MAX_SWAP_GROWTH_MIB:-256}"
max_load1="${LOAD_MAX_LOAD1:-6}"
probes="${LOAD_PROBES-studylife-demo.lktec.org homelabhub-demo.lktec.org piwatch-demo.lktec.org}"
probe_url="${LOAD_PROBE_URL:-http://127.0.0.1/}"
results="${LOAD_RESULTS:-./load-results/$(date -u +%Y%m%dT%H%M%SZ)}"
container="mowdeck-loadtest-k6"
here="$(cd "$(dirname "$0")" && pwd)"
load_dir="$(dirname "$here")"

die() {
  echo "ladder: $*" >&2
  exit 2
}

levels=("$@")
[ "${#levels[@]}" -gt 0 ] || levels=(10 20 30 40)
for level in "${levels[@]}"; do
  [[ "$level" =~ ^[1-9][0-9]{0,2}$ ]] || die "a level is a number of visitors (1 to 999): $level"
done

# Only a gateway on a Docker network: an internal name or a private address, never a public host.
host="${base_url#http://}"
host="${host%%/*}"
host="${host%%:*}"
[[ "$base_url" == http://* ]] || die "LOAD_BASE_URL must be http://<gateway on the proxy's network>"
if [[ "$host" == *.* ]] && ! [[ "$host" =~ ^(10|172\.(1[6-9]|2[0-9]|3[01])|192\.168)\. ]]; then
  die "LOAD_BASE_URL must name the gateway on a Docker network, not $host"
fi

command -v docker >/dev/null || die "docker is missing"
command -v curl >/dev/null || die "curl is missing"
[ "$(docker inspect -f '{{.State.Running}}' "$proxy" 2>/dev/null)" = true ] ||
  die "the proxy container $proxy is not running (LOAD_PROXY_CONTAINER)"
if docker inspect "$container" >/dev/null 2>&1; then
  die "a container $container exists (an earlier run?): docker rm -f $container"
fi

# The demo's reset and update wait for this lock (demosites mowdeck-demo/reset.sh), so no reset
# falls into the test. A reset or update running now refuses the test.
if [ -n "$lock" ] && [ -z "${LOAD_LOCKED:-}" ]; then
  [ -e "$lock" ] || : >"$lock" 2>/dev/null || die "cannot create $lock (LOAD_DEMO_LOCK= skips it)"
  export LOAD_LOCKED=1
  echo "ladder: holding $lock (exit code 75 at once: a reset or update of the demo is running)"
  exec flock --nonblock --conflict-exit-code 75 "$lock" "$0" "$@"
fi

postgres="$(docker ps -q --filter "label=com.docker.compose.project=$project" \
  --filter "label=com.docker.compose.service=postgres")"

sandboxes() {
  [ -n "$postgres" ] || return 0
  docker exec "$postgres" psql -U postgres -d identity -tAF= -c \
    "select state, count(*) from demo_sandboxes group by 1 order by 1" 2>/dev/null |
    tr '\n' ' ' || true
}

swap_used_mib() {
  awk '/^SwapTotal:/ {t = $2} /^SwapFree:/ {f = $2} END {printf "%d", (t - f) / 1024}' /proc/meminfo
}

probe() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H "Host: $1" "$probe_url" 2>/dev/null || true
}

echo "ladder: checking that the gateway answers from inside $proxy"
docker exec "$proxy" wget -q -O /dev/null -T 10 "$base_url/api/v1/identity/login-methods" ||
  die "$base_url does not answer from inside $proxy"
for h in $probes; do
  code="$(probe "$h")"
  [[ "$code" =~ ^[234] ]] || die "$h answers $code before the test: fix that first"
done

mkdir -p "$results"
results="$(cd "$results" && pwd)"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"; docker rm -f "$container" >/dev/null 2>&1 || true' EXIT
mkdir -p "$stage/load"
cp -R "$load_dir/lib" "$load_dir/scenarios" "$stage/load/"

swap_base="$(swap_used_mib)"
echo "ladder: levels ${levels[*]}, ramp $ramp, stay $duration, results in $results"
echo "ladder: swap in use $swap_base MiB, sandboxes: $(sandboxes)"

offset=0
status=0
for level in "${levels[@]}"; do
  dir="$results/level-$level"
  mkdir -p "$dir"
  offset=$((offset + 1000))
  echo
  echo "ladder: level $level ($(date -u +%H:%M:%SZ))"
  docker create --name "$container" --label mowdeck.loadtest=1 \
    --network "container:$proxy" --cpus "$k6_cpus" --memory "$k6_memory" \
    -w /tmp/load \
    -e "BASE_URL=$base_url" -e "ORIGIN=$origin" \
    -e LOAD_FORWARDED_PROTO=https \
    -e SUMMARY_FILE=/tmp/summary.json -e SUMMARY_ROW_FILE=/tmp/row.csv \
    -e "LOAD_VUS=$level" -e "LOAD_RAMP=$ramp" -e "LOAD_DURATION=$duration" \
    -e "LOAD_HOLD=$hold" -e "LOAD_ADDRESS_OFFSET=$offset" \
    -e "LOAD_MOW=${LOAD_MOW:-1}" \
    "$k6_image" run --quiet scenarios/sandbox-visitor.js >/dev/null
  tar -C "$stage" -cf - load | docker cp - "$container:/tmp"
  docker start "$container" >/dev/null
  docker logs -f "$container" 2>&1 | tee "$dir/k6.txt" &
  logs=$!

  reason=""
  declare -A failed=()
  while [ "$(docker inspect -f '{{.State.Running}}' "$container")" = true ]; do
    sleep 15
    growth=$(($(swap_used_mib) - swap_base))
    load1="$(cut -d' ' -f1 /proc/loadavg)"
    if [ "$growth" -ge "$max_swap_growth" ]; then
      reason="swap in use grew by $growth MiB"
    elif awk -v l="$load1" -v m="$max_load1" 'BEGIN {exit !(l > m)}'; then
      reason="load average $load1 above $max_load1"
    fi
    for h in $probes; do
      code="$(probe "$h")"
      if [[ "$code" =~ ^[234] ]]; then failed[$h]=0; else failed[$h]=$((${failed[$h]:-0} + 1)); fi
      [ "${failed[$h]}" -lt 2 ] || reason="$h answered $code twice in a row"
    done
    if [ -n "$reason" ]; then
      echo "ladder: stopping k6: $reason"
      docker kill -s INT "$container" >/dev/null 2>&1 || true
      break
    fi
  done
  code="$(docker wait "$container")"
  wait "$logs" 2>/dev/null || true
  echo
  docker cp "$container:/tmp/summary.json" "$dir/" 2>/dev/null || true
  docker cp "$container:/tmp/row.csv" "$dir/" 2>/dev/null || true
  docker rm "$container" >/dev/null
  unset failed

  if [ -f "$dir/row.csv" ]; then
    [ -f "$results/levels.csv" ] || head -n 1 "$dir/row.csv" >"$results/levels.csv"
    tail -n 1 "$dir/row.csv" >>"$results/levels.csv"
  fi
  echo "ladder: level $level ended with k6 exit code $code; waiting ${pause}s for the sandboxes to go"
  sleep "$pause"
  echo "ladder: sandboxes: $(sandboxes); swap in use $(swap_used_mib) MiB"

  if [ -n "$reason" ]; then
    echo "ladder: stopped at level $level: $reason"
    status=3
    break
  fi
  [ "$code" = 0 ] || status=1
  # A threshold crossed by latency alone is the knee coming, not a reason to stop: the next level
  # runs unless requests failed, live views broke or the API p95 passed LOAD_STOP_P95_MS.
  stop="$(tail -n 1 "$dir/row.csv" 2>/dev/null | awk -F, -v p95="$stop_p95" -v failed="$stop_failed" '{
    if ($3 == "" || $6 == "") print "no summary"
    else if ($6 + 0 >= failed + 0) print "failed requests " $6 " %"
    else if ($8 != "" && $8 + 0 < 99) print "live views ready " $8 " %"
    else if ($3 + 0 >= p95 + 0) print "API p95 " $3 " ms"
  }')"
  if [ "$code" != 0 ] && [ "$code" != 99 ]; then stop="${stop:-k6 exit code $code}"; fi
  if [ -n "$stop" ]; then
    echo "ladder: stopped after level $level: $stop"
    break
  fi
  [ "$code" = 0 ] || echo "ladder: level $level crossed a latency threshold; next level"
done

echo
[ -f "$results/levels.csv" ] && column -s, -t <"$results/levels.csv" 2>/dev/null ||
  cat "$results/levels.csv" 2>/dev/null || true
echo "ladder: results in $results"
exit "$status"

#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

# Resource sampler of the demo host during a load test (docs/ops/load-testing.md, "Measuring the
# real demo host"). Run it ON the host in its own shell before tests/load/vps/ladder.sh and stop it
# with Ctrl+C afterwards:
#
#   tests/load/vps/sample.sh [output directory]      default ./load-results/sample-<UTC time>
#
# Every SAMPLE_INTERVAL seconds (default 10) it appends
#
# - host.csv: load average, CPU busy / iowait / steal (percent of the whole host, all cores),
#   memory and swap in use, swap-in and swap-out pages per second, the demo's sandboxes per state
#   (Identity's demo_sandboxes table), whether the k6 container runs, and one probe per demo
#   through the host's proxy (status:seconds);
# - containers.csv: CPU (percent of one core, as docker stats shows it) and memory of every
#   container on the host, the other demos and k6 (mowdeck-loadtest-k6) included.
#
# It only reads: /proc, docker stats, one count query in the demo's database and a GET per demo.
# The sampler itself costs a little CPU (docker stats); its own share is in no column.
set -uo pipefail

interval="${SAMPLE_INTERVAL:-10}"
project="${SAMPLE_DEMO_PROJECT:-mowdeck-demo}"
probes="${SAMPLE_PROBES-mowdeck-demo.lktec.org studylife-demo.lktec.org homelabhub-demo.lktec.org piwatch-demo.lktec.org}"
probe_url="${SAMPLE_PROBE_URL:-http://127.0.0.1/}"
out="${1:-./load-results/sample-$(date -u +%Y%m%dT%H%M%SZ)}"

[[ "$interval" =~ ^[1-9][0-9]*$ ]] || {
  echo "sample: SAMPLE_INTERVAL must be whole seconds" >&2
  exit 2
}
command -v docker >/dev/null || {
  echo "sample: docker is missing" >&2
  exit 2
}
mkdir -p "$out"
host_csv="$out/host.csv"
containers_csv="$out/containers.csv"
[ -f "$host_csv" ] || echo "time_utc,cpus,load1,load5,cpu_busy_pct,cpu_iowait_pct,cpu_steal_pct,mem_used_mib,mem_available_mib,swap_used_mib,swap_in_pages_s,swap_out_pages_s,sandboxes_active,sandboxes_ready,sandboxes_provisioning,sandboxes_purging,k6_running,probes" >"$host_csv"
[ -f "$containers_csv" ] || echo "time_utc,container,cpu_pct,mem_mib" >"$containers_csv"

cpus="$(nproc 2>/dev/null || echo 0)"

# user nice system idle iowait irq softirq steal from the first line of /proc/stat
cpu_now() { awk '/^cpu / {print $2 + $3 + $4 + $7 + $8, $5, $6, $9; exit}' /proc/stat; }
swap_pages() { awk '/^pswpin / {i = $2} /^pswpout / {o = $2} END {print i + 0, o + 0}' /proc/vmstat; }

postgres=""
sandboxes() {
  if [ -z "$postgres" ]; then
    postgres="$(docker ps -q --filter "label=com.docker.compose.project=$project" \
      --filter "label=com.docker.compose.service=postgres" | head -n 1)"
  fi
  local rows=""
  [ -n "$postgres" ] && rows="$(docker exec "$postgres" psql -U postgres -d identity -tAF= -c \
    "select state, count(*) from demo_sandboxes group by 1" 2>/dev/null)" || postgres=""
  awk -F= -v rows="$rows" 'BEGIN {
    n = split(rows, lines, "\n")
    for (i = 1; i <= n; i++) { split(lines[i], kv, "="); c[kv[1]] = kv[2] }
    printf "%s,%s,%s,%s", c["active"] + 0, c["ready"] + 0, c["provisioning"] + 0, c["purging"] + 0
  }'
}

probe_all() {
  local h result list=""
  for h in $probes; do
    result="$(curl -s -o /dev/null -w '%{http_code}:%{time_total}' --max-time 5 \
      -H "Host: $h" "$probe_url" 2>/dev/null)" || true
    list="$list${list:+;}${h%%.*}=${result:-000:5}"
  done
  printf '%s' "$list"
}

trap 'echo; echo "sample: stopped, files in $out"; exit 0' INT TERM
echo "sample: every ${interval}s into $out (Ctrl+C stops)"

read -r busy0 idle0 iowait0 steal0 < <(cpu_now)
read -r in0 out0 < <(swap_pages)
while true; do
  sleep "$interval" &
  wait $!
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  read -r busy1 idle1 iowait1 steal1 < <(cpu_now)
  read -r in1 out1 < <(swap_pages)
  cpu="$(awk -v b="$((busy1 - busy0))" -v i="$((idle1 - idle0))" -v w="$((iowait1 - iowait0))" \
    -v s="$((steal1 - steal0))" 'BEGIN {
      t = b + i + w + s; if (t <= 0) t = 1
      printf "%.1f,%.1f,%.1f", 100 * b / t, 100 * w / t, 100 * s / t
    }')"
  swap_rate="$(awk -v i="$((in1 - in0))" -v o="$((out1 - out0))" -v d="$interval" \
    'BEGIN {printf "%.1f,%.1f", i / d, o / d}')"
  busy0=$busy1 idle0=$idle1 iowait0=$iowait1 steal0=$steal1 in0=$in1 out0=$out1
  read -r load1 load5 _ </proc/loadavg
  mem="$(awk '/^MemTotal:/ {t = $2} /^MemAvailable:/ {a = $2} /^SwapTotal:/ {st = $2}
    /^SwapFree:/ {sf = $2} END {printf "%d,%d,%d", (t - a) / 1024, a / 1024, (st - sf) / 1024}' /proc/meminfo)"
  k6=0
  docker inspect -f '{{.State.Running}}' mowdeck-loadtest-k6 2>/dev/null | grep -q true && k6=1
  echo "$now,$cpus,$load1,$load5,$cpu,$mem,$swap_rate,$(sandboxes),$k6,$(probe_all)" >>"$host_csv"
  docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}' 2>/dev/null |
    awk -F'|' -v now="$now" '{
      cpu = $2; sub(/%/, "", cpu)
      split($3, m, " / "); v = m[1]; n = v + 0
      if (v ~ /GiB$/) n *= 1024; else if (v ~ /KiB$/) n /= 1024; else if (v ~ /[0-9]B$/) n /= 1048576
      printf "%s,%s,%s,%.1f\n", now, $1, cpu, n
    }' >>"$containers_csv"
  tail -n 1 "$host_csv"
done

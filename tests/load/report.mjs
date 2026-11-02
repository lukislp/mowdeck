/**
 * Turns the samples of tests/load/load.mjs (docker stats, Postgres, NATS, Realtime metrics) and
 * k6 summaries into the Markdown tables of docs/ops/load-testing.md. Pure functions, tested by
 * report.test.mjs (node --test tests/load).
 */

const UNITS = {
  b: 1,
  kb: 1e3,
  mb: 1e6,
  gb: 1e9,
  kib: 1024,
  mib: 1024 ** 2,
  gib: 1024 ** 3,
};

/** "45.2MiB" -> bytes; NaN when it cannot be read. */
export function parseBytes(text) {
  const match = /^\s*([\d.]+)\s*([a-z]+)\s*$/i.exec(text ?? '');
  if (!match) return Number.NaN;
  const factor = UNITS[match[2].toLowerCase()];
  return factor ? Number(match[1]) * factor : Number.NaN;
}

/** One line of `docker stats --format '{{json .}}'` -> {service, cpu, memMiB, limitMiB}. */
export function parseStatsLine(line, nameToService = {}) {
  let row;
  try {
    row = JSON.parse(line);
  } catch {
    return null;
  }
  const [used, limit] = String(row.MemUsage ?? '').split('/');
  return {
    service: nameToService[row.Name] ?? row.Name,
    cpu: Number.parseFloat(String(row.CPUPerc ?? '').replace('%', '')) || 0,
    memMiB: parseBytes(used) / 1024 ** 2,
    limitMiB: parseBytes(limit) / 1024 ** 2,
  };
}

/** Prometheus text -> {"name{labels}": value}. */
export function parseMetrics(text) {
  const out = {};
  for (const line of String(text ?? '').split('\n')) {
    const match = /^([a-zA-Z_:][\w:]*(?:\{[^}]*\})?)\s+([-+\deE.]+|NaN)$/.exec(line.trim());
    if (match) out[match[1]] = Number(match[2]);
  }
  return out;
}

const round = (value, digits = 0) => {
  const f = 10 ** digits;
  return Math.round(value * f) / f;
};

/** Peak and average per service and for the whole stack over all samples. */
export function summariseSamples(samples) {
  const services = {};
  let totalCpuMax = 0;
  let totalCpuSum = 0;
  let totalMemMax = 0;
  const postgres = { peakConnections: 0, peakActive: 0, maxConnections: null };
  const nats = { peakConnections: 0, slowConsumers: 0, peakCpu: 0, peakMemMiB: 0 };
  const realtime = { peakConnections: 0, counters: {} };
  for (const s of samples) {
    let cpu = 0;
    let mem = 0;
    for (const c of s.containers ?? []) {
      const entry = (services[c.service] ??= {
        cpuMax: 0,
        cpuSum: 0,
        n: 0,
        memMaxMiB: 0,
        limitMiB: 0,
      });
      entry.cpuMax = Math.max(entry.cpuMax, c.cpu);
      entry.cpuSum += c.cpu;
      entry.n += 1;
      if (Number.isFinite(c.memMiB)) entry.memMaxMiB = Math.max(entry.memMaxMiB, c.memMiB);
      if (Number.isFinite(c.limitMiB)) entry.limitMiB = c.limitMiB;
      cpu += c.cpu;
      if (Number.isFinite(c.memMiB)) mem += c.memMiB;
    }
    totalCpuMax = Math.max(totalCpuMax, cpu);
    totalCpuSum += cpu;
    totalMemMax = Math.max(totalMemMax, mem);
    const [all, active, max] = String(s.postgres ?? '')
      .split('|')
      .map(Number);
    if (Number.isFinite(all)) postgres.peakConnections = Math.max(postgres.peakConnections, all);
    if (Number.isFinite(active)) postgres.peakActive = Math.max(postgres.peakActive, active);
    if (Number.isFinite(max) && max > 0) postgres.maxConnections = max;
    if (s.nats) {
      try {
        const v = JSON.parse(s.nats);
        nats.peakConnections = Math.max(nats.peakConnections, v.connections ?? 0);
        nats.slowConsumers = Math.max(nats.slowConsumers, v.slow_consumers ?? 0);
        nats.peakCpu = Math.max(nats.peakCpu, v.cpu ?? 0);
        nats.peakMemMiB = Math.max(nats.peakMemMiB, (v.mem ?? 0) / 1024 ** 2);
      } catch {
        // a sample taken while NATS restarts
      }
    }
    const metrics = parseMetrics(s.realtime);
    if ('mowdeck_realtime_connections' in metrics) {
      realtime.peakConnections = Math.max(
        realtime.peakConnections,
        metrics.mowdeck_realtime_connections,
      );
    }
    for (const [key, value] of Object.entries(metrics)) {
      if (/_total\{/.test(key)) realtime.counters[key] = value;
    }
  }
  const n = samples.length || 1;
  return {
    samples: samples.length,
    services: Object.fromEntries(
      Object.entries(services).map(([name, e]) => [
        name,
        {
          cpuAvg: round(e.cpuSum / (e.n || 1), 1),
          cpuMax: round(e.cpuMax, 1),
          memMaxMiB: round(e.memMaxMiB),
          limitMiB: round(e.limitMiB),
        },
      ]),
    ),
    totalCpuMax: round(totalCpuMax, 1),
    totalCpuAvg: round(totalCpuSum / n, 1),
    totalMemMaxMiB: round(totalMemMax),
    postgres,
    nats: { ...nats, peakMemMiB: round(nats.peakMemMiB) },
    realtime,
  };
}

/** The busiest service by peak CPU: [name, entry] or null. */
export function topService(stats) {
  const entries = Object.entries(stats.services ?? {});
  if (!entries.length) return null;
  return entries.reduce((a, b) => (b[1].cpuMax > a[1].cpuMax ? b : a));
}

export function formatStats(stats) {
  const lines = [
    `Resource usage over ${stats.samples} samples (CPU in % of one core; the stack shares 2 cores = 200 %)`,
    '',
    '| Service | CPU avg % | CPU peak % | Memory peak MiB | Limit MiB |',
    '| --- | ---: | ---: | ---: | ---: |',
  ];
  const sorted = Object.entries(stats.services).sort((a, b) => b[1].cpuMax - a[1].cpuMax);
  for (const [name, e] of sorted) {
    lines.push(`| ${name} | ${e.cpuAvg} | ${e.cpuMax} | ${e.memMaxMiB} | ${e.limitMiB} |`);
  }
  lines.push(
    `| **stack** | ${stats.totalCpuAvg} | ${stats.totalCpuMax} | ${stats.totalMemMaxMiB} | |`,
    '',
    `Postgres client connections: peak ${stats.postgres.peakConnections} ` +
      `(active ${stats.postgres.peakActive}) of max_connections ${stats.postgres.maxConnections ?? '?'}`,
    `NATS: peak ${stats.nats.peakConnections} connections, ${stats.nats.slowConsumers} slow consumers, ` +
      `peak CPU ${stats.nats.peakCpu} %, peak memory ${stats.nats.peakMemMiB} MiB`,
    `Realtime: peak ${stats.realtime.peakConnections} sockets; counters at the end: ` +
      (Object.entries(stats.realtime.counters)
        .filter(([key]) => /dropped|closed/.test(key))
        .map(([key, value]) => `${key.replace('mowdeck_realtime_', '')}=${value}`)
        .join(', ') || 'none'),
    '',
  );
  return lines.join('\n');
}

const metric = (summary, name, stat) => summary?.metrics?.[name]?.values?.[stat];

/**
 * Latency per request name (the k6 `name` tag) from a k6 summary, slowest p95 first. k6 only
 * keeps the sub-metrics that a threshold names, which is why the scenarios add a no-op threshold
 * per request name (lib/mowdeck.js, endpointThresholds).
 */
export function endpointTable(summary) {
  const rows = Object.entries(summary?.metrics ?? {})
    .map(([key, value]) => [/^http_req_duration\{name:(.+)\}$/.exec(key)?.[1], value?.values ?? {}])
    .filter(([name]) => name)
    .sort((a, b) => (b[1]['p(95)'] ?? 0) - (a[1]['p(95)'] ?? 0));
  const lines = ['| Request | avg ms | p95 ms | max ms |', '| --- | ---: | ---: | ---: |'];
  for (const [name, v] of rows) {
    lines.push(
      `| ${name.replaceAll('|', '\\|')} | ${ms(v.avg)} | ${ms(v['p(95)'])} | ${ms(v.max)} |`,
    );
  }
  return rows.length ? `${lines.join('\n')}\n` : '';
}
const ms = (value) => (Number.isFinite(value) ? `${Math.round(value)}` : 'n/a');
const pct = (value) => (Number.isFinite(value) ? `${round(value * 100, 2)}` : 'n/a');

export const SWEEP_HEADER = [
  '| Visitors | API p95 ms | Static p95 ms | HTTP errors % | WS ready % | WS with state % | States/s | Stack CPU peak % | Busiest service (peak %) | Memory peak MiB |',
  '| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |',
].join('\n');

/**
 * One row of the sweep table. `broken` names why the level is past the breaking point
 * (errors above 5 %, fewer than 95 % live sockets, API p95 above 2 s), else null.
 */
export function sweepRow(vus, summary, stats) {
  const apiP95 = metric(summary, 'http_req_duration{kind:api}', 'p(95)');
  const staticP95 = metric(summary, 'http_req_duration{kind:static}', 'p(95)');
  const failed = metric(summary, 'http_req_failed', 'rate');
  const wsReady = metric(summary, 'ws_connect_success', 'rate');
  const wsState = metric(summary, 'ws_sessions_with_state', 'rate');
  const states = metric(summary, 'ws_state_messages', 'rate');
  const top = topService(stats ?? {});
  const line =
    `| ${vus} | ${ms(apiP95)} | ${ms(staticP95)} | ${pct(failed)} | ${pct(wsReady)} | ${pct(wsState)} | ` +
    `${Number.isFinite(states) ? round(states, 1) : 'n/a'} | ${stats?.totalCpuMax ?? 'n/a'} | ` +
    `${top ? `${top[0]} (${top[1].cpuMax})` : 'n/a'} | ${stats?.totalMemMaxMiB ?? 'n/a'} |`;
  let broken = null;
  if (!summary) broken = 'no k6 summary';
  else if (failed > 0.05) broken = `HTTP errors ${pct(failed)} %`;
  else if (wsReady < 0.95) broken = `only ${pct(wsReady)} % live sockets`;
  else if (apiP95 > 2000) broken = `API p95 ${ms(apiP95)} ms`;
  return { line, broken };
}

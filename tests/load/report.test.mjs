import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  endpointTable,
  formatStats,
  parseBytes,
  parseMetrics,
  parseStatsLine,
  summariseSamples,
  sweepRow,
  topService,
} from './report.mjs';

test('parseBytes reads docker stats units', () => {
  assert.equal(parseBytes('512MiB'), 512 * 1024 ** 2);
  assert.equal(parseBytes(' 1.5GiB '), 1.5 * 1024 ** 3);
  assert.equal(parseBytes('100kB'), 100_000);
  assert.ok(Number.isNaN(parseBytes('lots')));
});

test('parseStatsLine maps the container to its service', () => {
  const line = JSON.stringify({
    Name: 'mowdeck-load-realtime-1',
    CPUPerc: '37.25%',
    MemUsage: '64MiB / 512MiB',
  });
  assert.deepEqual(parseStatsLine(line, { 'mowdeck-load-realtime-1': 'realtime' }), {
    service: 'realtime',
    cpu: 37.25,
    memMiB: 64,
    limitMiB: 512,
  });
  assert.equal(parseStatsLine('not json'), null);
});

test('parseMetrics reads Prometheus text with labels', () => {
  const metrics = parseMetrics(
    [
      '# HELP mowdeck_realtime_connections Open sockets',
      'mowdeck_realtime_connections 12.0',
      'mowdeck_realtime_closed_total{code="4429"} 3.0',
    ].join('\n'),
  );
  assert.equal(metrics.mowdeck_realtime_connections, 12);
  assert.equal(metrics['mowdeck_realtime_closed_total{code="4429"}'], 3);
});

test('summariseSamples keeps peaks, averages and the last counters', () => {
  const samples = [
    {
      containers: [
        { service: 'realtime', cpu: 50, memMiB: 100, limitMiB: 512 },
        { service: 'postgres', cpu: 10, memMiB: 200, limitMiB: 1280 },
      ],
      postgres: '20|2|100',
      nats: JSON.stringify({ connections: 14, slow_consumers: 0, cpu: 3, mem: 32 * 1024 ** 2 }),
      realtime: 'mowdeck_realtime_connections 40\nmowdeck_realtime_closed_total{code="none"} 1',
    },
    {
      containers: [
        { service: 'realtime', cpu: 150, memMiB: 120, limitMiB: 512 },
        { service: 'postgres', cpu: 30, memMiB: 210, limitMiB: 1280 },
      ],
      postgres: '35|7|100',
      nats: '',
      realtime: 'mowdeck_realtime_connections 80\nmowdeck_realtime_closed_total{code="none"} 4',
    },
  ];
  const stats = summariseSamples(samples);
  assert.deepEqual(stats.services.realtime, {
    cpuAvg: 100,
    cpuMax: 150,
    memMaxMiB: 120,
    limitMiB: 512,
  });
  assert.equal(stats.totalCpuMax, 180);
  assert.equal(stats.totalCpuAvg, 120);
  assert.equal(stats.totalMemMaxMiB, 330);
  assert.deepEqual(stats.postgres, { peakConnections: 35, peakActive: 7, maxConnections: 100 });
  assert.equal(stats.nats.peakConnections, 14);
  assert.equal(stats.nats.peakMemMiB, 32);
  assert.equal(stats.realtime.peakConnections, 80);
  assert.equal(stats.realtime.counters['mowdeck_realtime_closed_total{code="none"}'], 4);
  assert.deepEqual(topService(stats)[0], 'realtime');
  assert.match(formatStats(stats), /\| realtime \| 100 \| 150 \| 120 \| 512 \|/);
  assert.match(formatStats(stats), /closed_total\{code="none"\}=4/);
});

test('summariseSamples copes with no samples', () => {
  const stats = summariseSamples([]);
  assert.equal(stats.samples, 0);
  assert.equal(topService(stats), null);
});

const summary = (values) => ({
  metrics: Object.fromEntries(Object.entries(values).map(([name, v]) => [name, { values: v }])),
});

test('endpointTable lists named request latencies, slowest first', () => {
  const table = endpointTable(
    summary({
      'http_req_duration{name:GET /fleet/mowers}': { avg: 10.4, 'p(95)': 20.6, max: 30 },
      'http_req_duration{name:GET /telemetry/mowers/{id}/map}': { avg: 50, 'p(95)': 120, max: 300 },
      'http_req_duration{kind:api}': { avg: 1, 'p(95)': 999, max: 999 },
    }),
  );
  assert.equal(
    table,
    [
      '| Request | avg ms | p95 ms | max ms |',
      '| --- | ---: | ---: | ---: |',
      '| GET /telemetry/mowers/{id}/map | 50 | 120 | 300 |',
      '| GET /fleet/mowers | 10 | 21 | 30 |',
      '',
    ].join('\n'),
  );
  assert.equal(endpointTable(null), '');
});

test('sweepRow formats a level and detects the breaking point', () => {
  const ok = sweepRow(
    100,
    summary({
      'http_req_duration{kind:api}': { 'p(95)': 123.4 },
      'http_req_duration{kind:static}': { 'p(95)': 20.2 },
      http_req_failed: { rate: 0.001 },
      ws_connect_success: { rate: 1 },
      ws_sessions_with_state: { rate: 1 },
      ws_state_messages: { rate: 42.42 },
    }),
    {
      totalCpuMax: 150,
      totalMemMaxMiB: 3000,
      services: { realtime: { cpuMax: 80 }, identity: { cpuMax: 20 } },
    },
  );
  assert.equal(ok.line, '| 100 | 123 | 20 | 0.1 | 100 | 100 | 42.4 | 150 | realtime (80) | 3000 |');
  assert.equal(ok.broken, null);

  const errors = sweepRow(
    500,
    summary({ http_req_failed: { rate: 0.2 }, ws_connect_success: { rate: 1 } }),
    {},
  );
  assert.match(errors.broken, /HTTP errors 20/);
  const sockets = sweepRow(
    500,
    summary({ http_req_failed: { rate: 0 }, ws_connect_success: { rate: 0.5 } }),
    {},
  );
  assert.match(sockets.broken, /50 % live sockets/);
  assert.equal(sweepRow(1, null, null).broken, 'no k6 summary');
});

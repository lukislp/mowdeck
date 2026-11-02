#!/usr/bin/env node
/**
 * Load test of MowDeck against a throwaway Compose stack shaped like the public demo's host
 * (docs/ops/load-testing.md). Never point it at the public demo or any real installation: it
 * only ever talks to the stack it starts itself, inside that stack's own Docker network.
 *
 *   node tests/load/load.mjs up | down | ps | logs [service] | stats
 *   node tests/load/load.mjs k6 <scenario> [k6 args]   one scenario, with docker stats sampling
 *   node tests/load/load.mjs sweep                     demo visitors at LOAD_LEVELS, one run each
 *   node tests/load/load.mjs run                       up, LOAD_SCENARIOS, down (LOAD_KEEP=1 keeps)
 *
 * The stack is infra/compose/docker-compose.yml + docker-compose.demo.yml + docker-compose.load.yml
 * with infra/compose/.env.example and tests/load/load.env (plus MOWDECK_LOAD_ENV_FILE, e.g. the
 * image mirror lines in CI), as the Compose project MOWDECK_LOAD_PROJECT (default mowdeck-load)
 * with its own network, volumes, image tag and the host port MOWDECK_LOAD_PORT (default 8590,
 * 127.0.0.1 only). k6 runs in the official grafana/k6 image on the stack's network.
 */
import { spawn, spawnSync } from 'node:child_process';
import { createWriteStream, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  endpointTable,
  formatStats,
  parseStatsLine,
  summariseSamples,
  sweepRow,
  SWEEP_HEADER,
} from './report.mjs';

const loadDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(loadDir, '../..');
const composeDir = join(repoRoot, 'infra', 'compose');

const env = process.env;
const project = env.MOWDECK_LOAD_PROJECT ?? 'mowdeck-load';
const port = Number(env.MOWDECK_LOAD_PORT ?? '8590');
const origin = `http://localhost:${port}`;
const k6Image =
  env.K6_IMAGE ??
  'grafana/k6:1.3.0@sha256:3ddc8b1a33a2c3d8edc6e99b6a762ae36cba08788463458f5e6a7703e14eb77d';
const k6Cpus = env.LOAD_K6_CPUSET ?? '2-5';
const statsInterval = Number(env.LOAD_STATS_INTERVAL_SECONDS ?? '10') * 1000;
const scenarios = ['rate-limits', 'demo-visitor', 'rest-mix', 'command-burst'];

function fail(message) {
  console.error(`load: ${message}`);
  process.exit(2);
}

if (!/^[a-z0-9][a-z0-9_-]*$/.test(project)) fail(`invalid MOWDECK_LOAD_PROJECT ${project}`);
// Never the regular stack or the public demo's project.
if (['mowdeck', 'mowdeck-demo'].includes(project)) fail(`refusing the project ${project}`);
if (!Number.isInteger(port) || port < 1024 || port > 65_000) fail(`invalid MOWDECK_LOAD_PORT`);

const composeArgs = [
  'compose',
  '-p',
  project,
  '-f',
  join(composeDir, 'docker-compose.yml'),
  '-f',
  join(composeDir, 'docker-compose.demo.yml'),
  '-f',
  join(loadDir, 'docker-compose.load.yml'),
  // Optional experiment on top (a tuning to compare, docs/ops/load-testing.md).
  ...(env.MOWDECK_LOAD_COMPOSE_FILE ? ['-f', env.MOWDECK_LOAD_COMPOSE_FILE] : []),
  '--env-file',
  join(composeDir, '.env.example'),
  '--env-file',
  join(loadDir, 'load.env'),
  ...(env.MOWDECK_LOAD_ENV_FILE ? ['--env-file', env.MOWDECK_LOAD_ENV_FILE] : []),
];

const composeEnv = { ...env };
for (const name of [
  'COMPOSE_FILE',
  'COMPOSE_PROJECT_NAME',
  'COMPOSE_PROFILES',
  'COMPOSE_ENV_FILES',
]) {
  delete composeEnv[name];
}
Object.assign(composeEnv, {
  MOWDECK_NETWORK: project,
  MOWDECK_TRAEFIK_PROJECT: project,
  MOWDECK_VERSION: project,
  GATEWAY_HTTP_PORT: `127.0.0.1:${port}`,
  REALTIME_ALLOWED_ORIGINS: origin,
});

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { stdio: 'inherit', ...options });
  if (result.error) fail(`${command}: ${result.error.message}`);
  return result.status ?? 1;
}

function capture(command, args) {
  return new Promise((resolvePromise) => {
    const child = spawn(command, args, { stdio: ['ignore', 'pipe', 'ignore'] });
    let out = '';
    child.stdout.on('data', (chunk) => (out += chunk));
    child.on('error', () => resolvePromise(''));
    child.on('close', () => resolvePromise(out));
  });
}

const compose = (...args) => run('docker', [...composeArgs, ...args], { env: composeEnv });

function up() {
  const status = compose('up', '-d', '--build', '--wait', '--wait-timeout', '900');
  if (status === 0) console.log(`load: ${project} is up at ${origin}`);
  return status;
}

const down = () => compose('down', '-v', '--remove-orphans');

/** Running containers of the stack: [{name, service}]. */
function containers() {
  const result = spawnSync(
    'docker',
    [
      'ps',
      '--filter',
      `label=com.docker.compose.project=${project}`,
      '--format',
      '{{.Names}}\t{{.Label "com.docker.compose.service"}}',
    ],
    { encoding: 'utf8' },
  );
  return (result.stdout ?? '')
    .split('\n')
    .filter(Boolean)
    .map((line) => {
      const [name, service] = line.split('\t');
      return { name, service };
    });
}

/** One sample: docker stats of every container, Postgres connections, NATS, Realtime. */
async function sample(list) {
  const byService = Object.fromEntries(list.map((c) => [c.service, c.name]));
  const [stats, pg, nats, realtime] = await Promise.all([
    capture('docker', [
      'stats',
      '--no-stream',
      '--format',
      '{{json .}}',
      ...list.map((c) => c.name),
    ]),
    byService.postgres
      ? capture('docker', [
          'exec',
          byService.postgres,
          'psql',
          '-U',
          'postgres',
          '-tAc',
          "select count(*), count(*) filter (where state = 'active'), current_setting('max_connections') from pg_stat_activity where backend_type = 'client backend'",
        ])
      : '',
    byService.nats
      ? capture('docker', ['exec', byService.nats, 'wget', '-qO-', 'http://127.0.0.1:8222/varz'])
      : '',
    // Through the NATS container's wget: a Python process inside Realtime would cost the
    // measured service CPU time.
    byService.realtime && byService.nats
      ? capture('docker', [
          'exec',
          byService.nats,
          'wget',
          '-qO-',
          '-T',
          '5',
          'http://realtime:8000/metrics',
        ])
      : '',
  ]);
  const nameToService = Object.fromEntries(list.map((c) => [c.name, c.service]));
  return {
    at: new Date().toISOString(),
    containers: stats
      .split('\n')
      .filter(Boolean)
      .map((line) => parseStatsLine(line, nameToService))
      .filter(Boolean),
    postgres: pg.trim(),
    nats: nats.trim(),
    realtime: realtime
      .split('\n')
      .filter((line) => line.startsWith('mowdeck_realtime_') && !line.startsWith('#'))
      .join('\n'),
  };
}

function resultsDir(label) {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const dir = join(loadDir, 'results', `${stamp}-${label}`);
  mkdirSync(dir, { recursive: true });
  return dir;
}

/** Runs one k6 scenario while sampling the stack; returns {status, summary, stats}. */
async function k6(scenario, k6Args = [], extraEnv = {}, label = scenario) {
  if (!scenarios.includes(scenario)) fail(`unknown scenario ${scenario} (${scenarios.join(', ')})`);
  const dir = resultsDir(label);
  const list = containers();
  if (!list.length) fail(`${project} is not running (node tests/load/load.mjs up)`);
  const statsFile = join(dir, 'stats.jsonl');
  const statsOut = createWriteStream(statsFile);
  const samples = [];
  let sampling = true;
  const sampler = (async () => {
    while (sampling) {
      const started = Date.now();
      const s = await sample(list);
      samples.push(s);
      statsOut.write(`${JSON.stringify(s)}\n`);
      const wait = statsInterval - (Date.now() - started);
      if (wait > 0 && sampling) await new Promise((r) => setTimeout(r, wait));
    }
  })();

  const passEnv = Object.entries({ ...env, ...extraEnv })
    .filter(([name]) => /^LOAD_/.test(name) && !/^LOAD_(STACK|K6)_CPUSET$/.test(name))
    .flatMap(([name, value]) => ['-e', `${name}=${value}`]);
  const status = await new Promise((resolvePromise) => {
    const child = spawn(
      'docker',
      [
        'run',
        '--rm',
        '--network',
        project,
        '--cpuset-cpus',
        k6Cpus,
        // On Linux the results directory belongs to the caller: k6 writes its summary as them.
        ...(process.getuid ? ['--user', `${process.getuid()}:${process.getgid()}`] : []),
        '-v',
        `${loadDir}:/load:ro`,
        '-v',
        `${dir}:/results`,
        '-w',
        '/load',
        '-e',
        'BASE_URL=http://traefik',
        '-e',
        `ORIGIN=${origin}`,
        '-e',
        `SUMMARY_FILE=/results/summary.json`,
        ...passEnv,
        k6Image,
        'run',
        '--quiet',
        ...k6Args,
        `scenarios/${scenario}.js`,
      ],
      { stdio: ['ignore', 'pipe', 'inherit'] },
    );
    const log = createWriteStream(join(dir, 'k6.txt'));
    child.stdout.on('data', (chunk) => {
      process.stdout.write(chunk);
      log.write(chunk);
    });
    child.on('close', (code) => {
      log.end();
      resolvePromise(code ?? 1);
    });
  });
  sampling = false;
  await sampler;
  statsOut.end();
  const stats = summariseSamples(samples);
  writeFileSync(join(dir, 'stats.md'), formatStats(stats));
  console.log(formatStats(stats));
  const summaryPath = join(dir, 'summary.json');
  const summary = existsSync(summaryPath) ? JSON.parse(readFileSync(summaryPath, 'utf8')) : null;
  const endpoints = endpointTable(summary);
  if (endpoints) writeFileSync(join(dir, 'endpoints.md'), endpoints);
  console.log(`load: ${scenario} finished with ${status}, results in ${dir}`);
  return { status, summary, stats, dir };
}

/** Demo visitors at each of LOAD_LEVELS; one table row per level. */
async function sweep() {
  const levels = (env.LOAD_LEVELS ?? '25,50,100,200,300,400,500')
    .split(',')
    .map((v) => Number(v.trim()))
    .filter((v) => Number.isInteger(v) && v > 0);
  const rows = [SWEEP_HEADER];
  let worst = 0;
  for (const vus of levels) {
    const result = await k6(
      'demo-visitor',
      [],
      { LOAD_MODE: 'level', LOAD_VUS: String(vus) },
      `sweep-${vus}`,
    );
    const row = sweepRow(vus, result.summary, result.stats);
    rows.push(row.line);
    console.log(row.line);
    worst = Math.max(worst, result.status);
    if (row.broken && env.LOAD_SWEEP_CONTINUE !== '1') {
      console.log(`load: stopping the sweep at ${vus} visitors (${row.broken})`);
      break;
    }
  }
  const dir = resultsDir('sweep');
  writeFileSync(join(dir, 'sweep.md'), `${rows.join('\n')}\n`);
  console.log(rows.join('\n'));
  return worst;
}

async function runAll() {
  let status = up();
  if (status === 0) {
    const list = (env.LOAD_SCENARIOS ?? scenarios.join(',')).split(',').map((s) => s.trim());
    for (const scenario of list) {
      const result = scenario === 'sweep' ? { status: await sweep() } : await k6(scenario);
      status = Math.max(status, result.status);
    }
  }
  if (env.LOAD_KEEP === '1') console.log(`load: kept ${project} (node tests/load/load.mjs down)`);
  else down();
  return status;
}

const commands = {
  up,
  down,
  ps: () => compose('ps'),
  logs: (args) => compose('logs', '--no-color', '--timestamps', ...args),
  stats: async () => {
    console.log(formatStats(summariseSamples([await sample(containers())])));
    return 0;
  },
  k6: async ([scenario, ...rest]) => (await k6(scenario, rest)).status,
  sweep,
  run: runAll,
};

const [name, ...rest] = process.argv.slice(2);
if (!name || !(name in commands)) {
  console.log(
    [
      'usage: node tests/load/load.mjs <command>',
      '',
      '  up | down | ps | logs [service] | stats',
      `  k6 <scenario> [k6 args]   ${scenarios.join(', ')}`,
      '  sweep                     demo visitors at LOAD_LEVELS (default 25,...,500)',
      '  run                       up, LOAD_SCENARIOS (default all four), down',
      '',
      `project ${project} (MOWDECK_LOAD_PROJECT), gateway ${origin} (MOWDECK_LOAD_PORT)`,
    ].join('\n'),
  );
  process.exit(name ? 2 : 0);
}
process.exitCode = await commands[name](rest);

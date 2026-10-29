#!/usr/bin/env node
/**
 * Throwaway Compose stack for the e2e suites (tests/e2e-web and tests/e2e), next to any other
 * MowDeck stack on the same host and without touching it.
 *
 *   pnpm e2e:stack up | test | screens | acceptance | down | run | ps | logs | compose ...
 *
 * The stack is infra/compose/docker-compose.yml plus docker-compose.e2e.yml (the e2e marker),
 * configured only from infra/compose/.env.example and tests/e2e-web/stack.env (never from an
 * infra/compose/.env), as the Compose project MOWDECK_E2E_PROJECT (default mowdeck-e2e) with its
 * own network, volumes, image tag and host ports from MOWDECK_E2E_PORT (default 8480, bound to
 * 127.0.0.1): web at http://localhost:<port>, PostgreSQL at <port>+2 and so on, the camera
 * relay's ICE port (UDP and TCP) at <port>+9.
 */
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const suiteDir = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repoRoot = resolve(suiteDir, '../..');
const composeDir = join(repoRoot, 'infra', 'compose');

const project = process.env.MOWDECK_E2E_PROJECT ?? 'mowdeck-e2e';
const port = Number(process.env.MOWDECK_E2E_PORT ?? '8480');
const baseURL = `http://localhost:${port}`;

function fail(message) {
  console.error(`e2e:stack: ${message}`);
  process.exit(2);
}

if (!/^[a-z0-9][a-z0-9_-]*$/.test(project)) fail(`invalid MOWDECK_E2E_PROJECT ${project}`);
// The default project of docker-compose.yml is the stack people use: never manage it from here.
if (project === 'mowdeck') fail('MOWDECK_E2E_PROJECT must not be "mowdeck" (the regular stack)');
if (!Number.isInteger(port) || port < 1024 || port > 65_000 || port === 80) {
  fail(`invalid MOWDECK_E2E_PORT ${process.env.MOWDECK_E2E_PORT}`);
}

// MOWDECK_E2E_DEMO=1: the public demo (ADR 0065) on top, with the test secrets of demo.env;
// `pnpm e2e:stack demo` runs its specs. A demo stack is only for the demo specs.
const demo = process.env.MOWDECK_E2E_DEMO === '1';

const composeArgs = [
  'compose',
  '-p',
  project,
  '-f',
  join(composeDir, 'docker-compose.yml'),
  '-f',
  join(composeDir, 'docker-compose.e2e.yml'),
  ...(demo ? ['-f', join(composeDir, 'docker-compose.demo.yml')] : []),
  '--env-file',
  join(composeDir, '.env.example'),
  '--env-file',
  join(suiteDir, 'stack.env'),
  ...(demo ? ['--env-file', join(suiteDir, 'demo.env')] : []),
  // Optional extra settings, e.g. image mirror lines (later files win).
  ...(process.env.MOWDECK_E2E_ENV_FILE ? ['--env-file', process.env.MOWDECK_E2E_ENV_FILE] : []),
];

// Exported variables override the env files: these make the stack independent of any other.
const composeEnv = { ...process.env };
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
  // Own image tag: building here never replaces the images of another stack (mowdeck/*:dev).
  MOWDECK_VERSION: project,
  GATEWAY_HTTP_PORT: `127.0.0.1:${port}`,
  // Passkeys (ADR 0050) are bound to the address the browser opens: http://localhost works
  // without HTTPS. Replaces stack.env's IDENTITY_PUBLIC_URL (the CI address, port 80).
  IDENTITY_PUBLIC_URL: baseURL,
  GATEWAY_HTTPS_PORT: `127.0.0.1:${port + 1}`,
  POSTGRES_PORT: String(port + 2),
  NATS_CLIENT_PORT: String(port + 3),
  NATS_MONITOR_PORT: String(port + 4),
  OPENFGA_HTTP_PORT: String(port + 5),
  OPENFGA_GRPC_PORT: String(port + 6),
  // The camera relay's ICE port (UDP and TCP): container port = host port, see mediamtx.yml.
  RELAY_ICE_BIND: '127.0.0.1',
  RELAY_ICE_PORT: String(port + 9),
  RELAY_ICE_HOSTS: '127.0.0.1',
  REALTIME_ALLOWED_ORIGINS: `${baseURL},http://127.0.0.1:${port}`,
});

const suiteEnv = {
  ...process.env,
  MOWDECK_E2E_BASE_URL: baseURL,
  MOWDECK_E2E_COMPOSE_PROJECT: project,
};

function run(command, args, options) {
  const result = spawnSync(command, args, { stdio: 'inherit', ...options });
  if (result.error) fail(`${command}: ${result.error.message}`);
  return result.status ?? 1;
}

const compose = (...args) => run('docker', [...composeArgs, ...args], { env: composeEnv });

function playwright(args) {
  const cli = createRequire(join(suiteDir, 'package.json')).resolve('@playwright/test/cli');
  return run(process.execPath, [cli, 'test', ...args], { cwd: suiteDir, env: suiteEnv });
}

function suite(args) {
  const projects = args.some((arg) => arg.startsWith('--project'))
    ? []
    : ['--project=phone', '--project=desktop'];
  return playwright([...projects, ...args]);
}

function up() {
  const status = compose('up', '-d', '--build', '--wait', '--wait-timeout', '600');
  if (status === 0) console.log(`e2e:stack: ${project} is up at ${baseURL}`);
  return status;
}

const down = () => compose('down', '-v', '--remove-orphans');

const commands = {
  up,
  down,
  test: suite,
  screens: (args) => playwright(['--project=screens', ...args]),
  // The public demo's specs (start the stack with MOWDECK_E2E_DEMO=1).
  demo: (args) => playwright(['--project=demo', '--project=demo-phone', ...args]),
  // The Phase 1 acceptance test (pytest) against the same stack.
  acceptance: (args) => run('uv', ['run', 'poe', 'e2e', ...args], { cwd: repoRoot, env: suiteEnv }),
  // up, the Playwright suite, down (the stack stays with MOWDECK_E2E_KEEP=1).
  run: (args) => {
    let status = up();
    if (status === 0) status = suite(args);
    if (process.env.MOWDECK_E2E_KEEP === '1') {
      console.log(`e2e:stack: kept ${project} at ${baseURL} (pnpm e2e:stack down removes it)`);
    } else {
      down();
    }
    return status;
  },
  ps: () => compose('ps'),
  logs: (args) => compose('logs', '--no-color', '--timestamps', ...args),
  compose: (args) => compose(...args),
};

const [name, ...rest] = process.argv.slice(2);
if (!name || !(name in commands)) {
  console.log(
    [
      'usage: pnpm e2e:stack <command> [args]',
      '',
      '  up          build and start the throwaway stack, wait until healthy',
      '  test        Playwright projects phone and desktop (extra args go to playwright test)',
      '  screens     Playwright project screens (review screenshots)',
      '  demo        the public demo specs (stack started with MOWDECK_E2E_DEMO=1)',
      '  acceptance  the Phase 1 acceptance test tests/e2e (uv run poe e2e)',
      '  down        stop the stack and delete its volumes',
      '  run         up, test, down (MOWDECK_E2E_KEEP=1 keeps the stack)',
      '  ps | logs [service] | compose <args>',
      '',
      `project ${project} (MOWDECK_E2E_PROJECT), web at ${baseURL} (MOWDECK_E2E_PORT)`,
    ].join('\n'),
  );
  process.exit(name ? 2 : 0);
}
process.exit(commands[name](rest));

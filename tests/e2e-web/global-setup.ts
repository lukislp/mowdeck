/**
 * Safety guard: the suite registers users, accounts and simulated mowers, so it only runs against
 * a throwaway test stack. Such a stack carries the marker of infra/compose/docker-compose.e2e.yml
 * at /api/v1/e2e-stack, which answers with its Compose project name as the host name. Any other
 * target (the stack you actually use, at http://localhost or on the LAN) is refused before a test
 * starts, unless MOWDECK_E2E_ALLOW_ANY_STACK=1 says so explicitly.
 *
 * The project name found here is handed to the tests as MOWDECK_E2E_COMPOSE_PROJECT (workers
 * inherit the environment of the global setup), so `docker exec` helpers reach the same stack.
 */
import type { FullConfig } from '@playwright/test';

const MARKER = '/api/v1/e2e-stack';
const START_HINT =
  'Start the isolated test stack with `pnpm e2e:stack up` (Compose project mowdeck-e2e at ' +
  'http://localhost:8480) or run everything at once with `pnpm e2e:stack run`; see ' +
  'tests/e2e-web/README.md.';

/** The Compose project named by the e2e marker at `baseURL`, or null when there is no marker. */
async function markerProject(baseURL: string): Promise<string | null> {
  let response: Response;
  try {
    response = await fetch(new URL(MARKER, baseURL), { signal: AbortSignal.timeout(10_000) });
  } catch (error) {
    throw new Error(`No stack answers at ${baseURL}. ${START_HINT}`, { cause: error });
  }
  if (!response.ok) return null;
  // A stack without the marker serves the web app's index.html here (status 200), so the body
  // decides: whoami's "Hostname: <project>" line.
  const match = /^Hostname:\s*(\S+)\s*$/m.exec(await response.text());
  return match ? match[1] : null;
}

export default async function globalSetup(config: FullConfig): Promise<void> {
  const baseURL = config.projects[0]?.use.baseURL;
  if (!baseURL) throw new Error('playwright.config.ts sets no baseURL');

  const project = await markerProject(baseURL);
  if (project) {
    const named = process.env.MOWDECK_E2E_COMPOSE_PROJECT;
    if (named && named !== project) {
      throw new Error(
        `MOWDECK_E2E_COMPOSE_PROJECT=${named}, but the stack at ${baseURL} is the Compose ` +
          `project ${project}: the docker exec helpers would reach another stack.`,
      );
    }
    process.env.MOWDECK_E2E_COMPOSE_PROJECT = project;
    console.log(`e2e target: ${baseURL} (Compose project ${project})`);
    return;
  }

  if (process.env.MOWDECK_E2E_ALLOW_ANY_STACK === '1') {
    console.warn(
      `e2e target: ${baseURL} has no e2e marker; running anyway (MOWDECK_E2E_ALLOW_ANY_STACK=1). ` +
        'The suite will create users, accounts and mowers there.',
    );
    return;
  }

  throw new Error(
    [
      `Refusing to run the e2e suite against ${baseURL}: it is not an isolated test stack`,
      `(no e2e marker at ${MARKER}). The suite creates users, accounts and simulated mowers and`,
      'must never write into the stack you use.',
      START_HINT,
      'To target another stack on purpose, set MOWDECK_E2E_ALLOW_ANY_STACK=1.',
    ].join('\n'),
  );
}

/**
 * The server's small HTTP API, behind Traefik at `/api/v1/matter` (Compose: the host network,
 * reached through `host.docker.internal`; Kubernetes: the pod's Service):
 *
 * | Method | Path | Who | Answer |
 * | --- | --- | --- | --- |
 * | GET | `/api/v1/matter/mowers` | token | The mowers with a node the caller may control: name, commissioned, number of controllers |
 * | GET | `/api/v1/matter/mowers/{id}/pairing` | `can_control` | QR code (payload and modules) and the 11-digit manual code; `Cache-Control: no-store` |
 * | POST | `/api/v1/matter/mowers/{id}/pairing/reset` | `can_control` | Factory reset of the node with a new code; every controller loses the mower |
 * | GET | `/healthz`, `/readyz`, `/metrics`, `/.well-known/mowdeck-module` | anyone | Operations and the module descriptor (the settings section of the web app) |
 *
 * The caller's token is checked by Identity (`POST /identity/me/permissions` with the caller's
 * token): a mower the caller cannot see answers 404, one the caller sees but may not control 403.
 * Codes are written only into the response body, never into a log line.
 */
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';

import { errorText, log } from './log.ts';
import { MowDeckError, type PermissionAnswer } from './mowdeck.ts';
import type { NodeManager, PairingInfo } from './nodes.ts';
import { qrMatrix } from './pairing.ts';
import { isMowerId } from './store.ts';

export type PermissionChecker = (
  authorization: string | undefined,
  mowerIds: readonly string[],
) => Promise<Map<string, PermissionAnswer>>;

export interface HttpOptions {
  manager: Pick<NodeManager, 'ready' | 'size' | 'summaries' | 'pairing' | 'reset'>;
  permissions: PermissionChecker;
  version: string;
  sourceUrl: string;
}

const PREFIX = '/api/v1/matter';
const PAIRING = new RegExp(`^${PREFIX}/mowers/([^/]+)/pairing(/reset)?$`);

/** The module descriptor (contracts `ModuleDescriptor`): the web app shows the "Matter" section
 * of the settings to users with `mower.control` once Identity lists this module. */
export function descriptor(version: string): Record<string, unknown> {
  return {
    name: 'matter-server',
    version: /^\d+\.\d+\.\d+/.test(version) ? version : '0.0.0',
    permissions: [],
    features: [],
    events: [],
    ui_areas: [
      {
        route: '/settings/matter',
        menu_label: 'Matter',
        labels: { de: 'Matter', en: 'Matter' },
        required_permission: 'mower.control',
      },
    ],
  };
}

function send(
  response: ServerResponse,
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): void {
  const text = typeof body === 'string' ? body : JSON.stringify(body);
  response.writeHead(status, {
    'Content-Type': typeof body === 'string' ? 'text/plain; charset=utf-8' : 'application/json',
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    ...headers,
  });
  response.end(text);
}

function problem(response: ServerResponse, status: number, slug: string, title: string): void {
  response.writeHead(status, {
    'Content-Type': 'application/problem+json',
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
  });
  response.end(JSON.stringify({ type: `urn:mowdeck:problem:${slug}`, title, status }));
}

export function pairingBody(info: PairingInfo): Record<string, unknown> {
  return {
    mower_id: info.mowerId,
    name: info.name,
    commissioned: info.commissioned,
    fabric_count: info.fabricCount,
    manual_code: info.codes.manual,
    qr_payload: info.codes.qrPayload,
    qr_matrix: qrMatrix(info.codes.qrPayload),
  };
}

export function createHttpHandler(options: HttpOptions) {
  const { manager, permissions } = options;
  let requests = 0;

  async function allowed(
    request: IncomingMessage,
    response: ServerResponse,
    mowerId: string,
  ): Promise<boolean> {
    const answer = (await permissions(request.headers.authorization, [mowerId])).get(mowerId);
    if (!answer?.view || !manager.pairing(mowerId)) {
      problem(response, 404, 'not-found', 'Mower not found');
      return false;
    }
    if (!answer.control) {
      problem(response, 403, 'forbidden', 'Controlling this mower is not allowed');
      return false;
    }
    return true;
  }

  async function route(request: IncomingMessage, response: ServerResponse): Promise<void> {
    const url = new URL(request.url ?? '/', 'http://matter-server');
    const path = url.pathname;
    const method = request.method ?? 'GET';
    if (method === 'GET' && path === '/healthz') return send(response, 200, { status: 'ok' });
    if (method === 'GET' && path === '/readyz') {
      return manager.ready
        ? send(response, 200, { status: 'ok' })
        : send(response, 503, { status: 'starting' });
    }
    if (method === 'GET' && path === '/metrics') {
      return send(
        response,
        200,
        [
          '# HELP mowdeck_matter_nodes Matter nodes served (one per mower).',
          '# TYPE mowdeck_matter_nodes gauge',
          `mowdeck_matter_nodes ${manager.size}`,
          '# HELP mowdeck_matter_http_requests_total Requests to the admin API.',
          '# TYPE mowdeck_matter_http_requests_total counter',
          `mowdeck_matter_http_requests_total ${requests}`,
          '',
        ].join('\n'),
        { 'Content-Type': 'text/plain; version=0.0.4' },
      );
    }
    if (method === 'GET' && path === '/.well-known/mowdeck-module') {
      return send(response, 200, descriptor(options.version));
    }
    if (!path.startsWith(`${PREFIX}/`)) return problem(response, 404, 'not-found', 'Not found');
    requests++;

    if (path === `${PREFIX}/mowers`) {
      if (method !== 'GET') return problem(response, 405, 'method-not-allowed', 'Not allowed');
      const nodes = manager.summaries();
      const answers = await permissions(
        request.headers.authorization,
        nodes.map((n) => n.mowerId),
      );
      const items = nodes
        .filter((n) => answers.get(n.mowerId)?.control === true)
        .sort((a, b) => a.name.localeCompare(b.name))
        .map((n) => ({
          mower_id: n.mowerId,
          name: n.name,
          commissioned: n.commissioned,
          fabric_count: n.fabricCount,
        }));
      return send(response, 200, { items });
    }

    const match = PAIRING.exec(path);
    if (!match?.[1] || !isMowerId(match[1])) {
      return problem(response, 404, 'not-found', 'Not found');
    }
    const mowerId = match[1].toLowerCase();
    const reset = match[2] === '/reset';
    if ((reset && method !== 'POST') || (!reset && method !== 'GET')) {
      return problem(response, 405, 'method-not-allowed', 'Not allowed');
    }
    if (!(await allowed(request, response, mowerId))) return;
    const info = reset ? await manager.reset(mowerId) : manager.pairing(mowerId);
    if (!info) return problem(response, 409, 'conflict', 'The node is busy, try again');
    if (reset) log('info', 'matter.pairing.reset_requested', { mower_id: mowerId });
    return send(response, 200, pairingBody(info));
  }

  return async (request: IncomingMessage, response: ServerResponse): Promise<void> => {
    response.setHeader('X-Source-Code', options.sourceUrl);
    try {
      await route(request, response);
    } catch (error) {
      if (error instanceof MowDeckError && error.status === 401) {
        return problem(response, 401, 'unauthorized', 'Not signed in');
      }
      log('error', 'matter.http.failed', { path: request.url?.split('?')[0], error: errorText(error) });
      if (!response.headersSent) problem(response, 502, 'upstream', 'MowDeck did not answer');
    }
  };
}

export function startHttp(options: HttpOptions, host: string, port: number): Server {
  const handler = createHttpHandler(options);
  const server = createServer((request, response) => void handler(request, response));
  server.listen(port, host);
  return server;
}

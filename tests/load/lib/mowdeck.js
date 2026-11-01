// Shared helpers of the k6 scenarios (tests/load/scenarios). Only ever pointed at the throwaway
// stack of tests/load/load.mjs: BASE_URL is the stack's gateway inside its own Docker network.
import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import ws from 'k6/ws';
import { textSummary } from 'https://jslib.k6.io/k6-summary/0.1.0/index.js';

export const BASE = __ENV.BASE_URL || 'http://traefik';
export const ORIGIN = __ENV.ORIGIN || 'http://localhost:8590';
const WS_BASE = BASE.replace(/^http/, 'ws');

export const wsConnectSuccess = new Rate('ws_connect_success');
export const wsSessionsWithState = new Rate('ws_sessions_with_state');
export const wsStateMessages = new Counter('ws_state_messages');
export const wsReadyTime = new Trend('ws_ready_time', true);
export const wsFirstStateTime = new Trend('ws_first_state_time', true);
export const wsEarlyClose = new Counter('ws_early_close');
export const rateLimited = new Counter('rate_limited');

/**
 * The client address of a virtual visitor: `<prefix>.<hi>.<lo>` from a number. Every scenario
 * uses its own prefix, so visitors of concurrent scenarios never share an address. The gateway
 * trusts k6 as the demo host trusts its TLS proxy (tests/load/load.env) and counts every
 * per-address limit for this address.
 */
export function visitorAddress(prefix, n) {
  return `${prefix}.${(n >> 8) & 255}.${n & 255}`;
}

/** A visitor: its address, the access token and the request headers it sends. */
export class Visitor {
  constructor(address) {
    this.address = address;
    this.token = null;
    this.expiresAt = 0;
  }

  headers(extra = {}) {
    const h = { 'X-Forwarded-For': this.address, ...extra };
    if (this.token) h.Authorization = `Bearer ${this.token}`;
    return h;
  }

  params(name, kind = 'api', extra = {}) {
    return { headers: this.headers(extra), tags: { name, kind } };
  }

  get(path, name, kind = 'api') {
    return note(http.get(`${BASE}${path}`, this.params(name || path, kind)));
  }

  post(path, body, name, extra = {}) {
    return note(
      http.post(
        `${BASE}${path}`,
        body === undefined ? null : JSON.stringify(body),
        this.params(name || path, 'api', {
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
          ...extra,
        }),
      ),
    );
  }

  /** "Try the demo": a new login session of the shared demo user, as the web app asks for it. */
  signIn() {
    http.cookieJar().clear(BASE);
    this.token = null;
    const res = this.post('/api/v1/identity/demo/session', {}, 'POST /identity/demo/session', {
      'X-Requested-With': 'mowdeck-web',
      'X-Mowdeck-Refresh': 'cookie-only',
    });
    const ok = check(res, { 'demo session 200': (r) => r.status === 200 });
    if (!ok) return false;
    const body = res.json();
    this.token = body.access_token;
    this.expiresAt = Date.now() + (body.expires_in || 600) * 1000;
    return true;
  }

  /** Refreshes the access token 60 s before it expires, as the web app does. */
  refreshIfDue() {
    if (!this.token || Date.now() < this.expiresAt - 60_000) return true;
    const res = this.post(
      '/api/v1/identity/session/refresh',
      undefined,
      'POST /identity/session/refresh',
      { 'X-Requested-With': 'mowdeck-web', 'X-Mowdeck-Refresh': 'cookie-only' },
    );
    if (res.status !== 200) return this.signIn();
    const body = res.json();
    this.token = body.access_token;
    this.expiresAt = Date.now() + (body.expires_in || 600) * 1000;
    return true;
  }
}

function note(res) {
  if (res.status === 429) rateLimited.add(1);
  if (__ENV.LOAD_DEBUG && (res.status >= 400 || res.status === 0))
    console.warn(`${res.status} ${res.request.method} ${res.url} ${res.error || ''}`);
  return res;
}

/** http.batch with the bookkeeping of single requests (429 counter, LOAD_DEBUG). */
export function batch(requests) {
  const res = http.batch(requests);
  res.forEach(note);
  return res;
}

/** JSON body or null. */
export function json(res) {
  try {
    return res.json();
  } catch {
    return null;
  }
}

/** A list response as an array, whatever the envelope (`items`, `mowers`, a bare list). */
export function items(body, key) {
  if (Array.isArray(body)) return body;
  if (!body) return [];
  return body[key] || body.items || body.data || [];
}

/** The web app's static files of a first visit: index.html and what it preloads. */
export function loadApp(visitor) {
  const index = visitor.get('/', 'GET /', 'static');
  check(index, { 'index 200': (r) => r.status === 200 });
  const assets = new Set();
  const re = /(?:href|src)="(\/_app\/[^"]+)"/g;
  let m;
  while ((m = re.exec(String(index.body || ''))) !== null) assets.add(m[1]);
  const requests = [...assets].map((path) => [
    'GET',
    `${BASE}${path}`,
    null,
    { headers: visitor.headers(), tags: { name: 'GET /_app/*', kind: 'static' } },
  ]);
  requests.push([
    'GET',
    `${BASE}/config.json`,
    null,
    { headers: visitor.headers(), tags: { name: 'GET /config.json', kind: 'static' } },
  ]);
  for (const res of http.batch(requests)) note(res);
  return assets.size;
}

/** Calls of the signed-in app shell (routes/(app)/+layout.svelte). */
export function appShell(visitor) {
  const res = http.batch([
    ['GET', `${BASE}/api/v1/identity/me`, null, visitor.params('GET /identity/me')],
    ['GET', `${BASE}/api/v1/identity/ui-areas`, null, visitor.params('GET /identity/ui-areas')],
    [
      'GET',
      `${BASE}/api/v1/identity/me/platform`,
      null,
      visitor.params('GET /identity/me/platform'),
    ],
    ['GET', `${BASE}/api/v1/entitlements/me`, null, visitor.params('GET /entitlements/me')],
    // Only release builds ship it; the app treats 404 as "nothing new".
    [
      'GET',
      `${BASE}/whats-new.json`,
      null,
      {
        ...visitor.params('GET /whats-new.json', 'static'),
        responseCallback: http.expectedStatuses(200, 404),
      },
    ],
    [
      'GET',
      `${BASE}/api/v1/notifications/inbox?limit=50`,
      null,
      visitor.params('GET /notifications/inbox'),
    ],
  ]);
  res.forEach(note);
  check(res[0], { 'me 200': (r) => r.status === 200 });
}

/** The mower list (/mowers); returns the mower ids. */
export function mowerList(visitor) {
  const res = http.batch([
    ['GET', `${BASE}/api/v1/fleet/mowers`, null, visitor.params('GET /fleet/mowers')],
    [
      'GET',
      `${BASE}/api/v1/telemetry/accounts/me/mowers/states`,
      null,
      visitor.params('GET /telemetry/accounts/me/mowers/states'),
    ],
    [
      'GET',
      `${BASE}/api/v1/telemetry/accounts/me/mowers/capabilities`,
      null,
      visitor.params('GET /telemetry/accounts/me/mowers/capabilities'),
    ],
  ]);
  res.forEach(note);
  check(res[0], { 'mowers 200': (r) => r.status === 200 });
  return items(json(res[0]), 'mowers')
    .map((m) => m.id || m.mower_id)
    .filter(Boolean);
}

const iso = (ms) => new Date(ms).toISOString();

/** The mower page (/mowers/[id]) on its first load; returns the map's area ids. */
export function mowerPage(visitor, id) {
  const p = (name) => visitor.params(name);
  const mower = visitor.get(`/api/v1/fleet/mowers/${id}`, 'GET /fleet/mowers/{id}');
  check(mower, { 'mower 200': (r) => r.status === 200 });
  const midnight = new Date();
  midnight.setUTCHours(0, 0, 0, 0);
  const now = Date.now();
  const res = http.batch([
    [
      'GET',
      `${BASE}/api/v1/telemetry/mowers/${id}/state`,
      null,
      p('GET /telemetry/mowers/{id}/state'),
    ],
    [
      'POST',
      `${BASE}/api/v1/identity/me/permissions`,
      JSON.stringify({
        checks: ['can_control', 'can_manage_mowers', 'can_drive'].map((relation) => ({
          relation,
          object: `mower:${id}`,
        })),
      }),
      visitor.params('POST /identity/me/permissions', 'api', {
        'Content-Type': 'application/json',
      }),
    ],
    [
      'GET',
      `${BASE}/api/v1/fleet/mowers/${id}/commands?limit=8`,
      null,
      p('GET /fleet/mowers/{id}/commands'),
    ],
    ['GET', `${BASE}/api/v1/scheduler/schedules`, null, p('GET /scheduler/schedules')],
    [
      'GET',
      `${BASE}/api/v1/telemetry/mowers/${id}/zones`,
      null,
      p('GET /telemetry/mowers/{id}/zones'),
    ],
    ['GET', `${BASE}/api/v1/telemetry/mowers/${id}/map`, null, p('GET /telemetry/mowers/{id}/map')],
    [
      'GET',
      `${BASE}/api/v1/telemetry/mowers/${id}/capabilities`,
      null,
      p('GET /telemetry/mowers/{id}/capabilities'),
    ],
    ['GET', `${BASE}/api/v1/scheduler/weather/${id}`, null, p('GET /scheduler/weather/{id}')],
    [
      'GET',
      `${BASE}/api/v1/telemetry/mowers/${id}/tracking?after=${iso(now)}`,
      null,
      p('GET /telemetry/mowers/{id}/tracking'),
    ],
    [
      'GET',
      `${BASE}/api/v1/telemetry/mowers/${id}/positions?from=${iso(midnight.getTime())}&limit=5000`,
      null,
      p('GET /telemetry/mowers/{id}/positions (today)'),
    ],
    [
      'GET',
      `${BASE}/api/v1/telemetry/mowers/${id}/positions?from=${iso(now - 7 * 86_400_000)}&to=${iso(now)}&limit=5000`,
      null,
      p('GET /telemetry/mowers/{id}/positions (7 days)'),
    ],
    [
      'GET',
      `${BASE}/api/v1/telemetry/mowers/${id}/aerial`,
      null,
      p('GET /telemetry/mowers/{id}/aerial'),
    ],
  ]);
  res.forEach(note);
  check(res[5], { 'map 200': (r) => r.status === 200 });
  const map = json(res[5]) || {};
  return (map.areas || (map.map && map.map.areas) || []).map((a) => a.id).filter(Boolean);
}

/**
 * Holds the live view (ADR 0018) for `holdMs`: auth, subscribe, application pings every 25 s,
 * the inbox poll every 60 s. Records whether the socket became ready and received states.
 */
export function liveView(visitor, mowerIds, holdMs) {
  const started = Date.now();
  let ready = false;
  let states = 0;
  let closedByUs = false;
  let n = 0;
  const res = ws.connect(
    `${WS_BASE}/api/v1/realtime/ws`,
    {
      headers: { Origin: ORIGIN, 'X-Forwarded-For': visitor.address },
      tags: { name: 'WS /realtime/ws' },
    },
    (socket) => {
      socket.on('open', () => socket.send(JSON.stringify({ type: 'auth', token: visitor.token })));
      socket.on('message', (raw) => {
        let msg;
        try {
          msg = JSON.parse(raw);
        } catch {
          return;
        }
        if (msg.type === 'ready' && !ready) {
          ready = true;
          wsReadyTime.add(Date.now() - started);
          socket.send(
            JSON.stringify({
              type: 'subscribe',
              request_id: 'r1',
              mower_ids: mowerIds.slice(0, 200),
            }),
          );
        } else if (msg.type === 'state') {
          states += 1;
          wsStateMessages.add(1);
          if (states === 1) wsFirstStateTime.add(Date.now() - started);
        }
      });
      socket.on('close', (code) => {
        if (!closedByUs) wsEarlyClose.add(1, { code: String(code) });
      });
      socket.setInterval(() => {
        n += 1;
        socket.send(JSON.stringify({ type: 'ping', request_id: `p${n}` }));
      }, 25_000);
      socket.setInterval(() => {
        visitor.get('/api/v1/notifications/inbox?limit=50', 'GET /notifications/inbox');
      }, 60_000);
      socket.setTimeout(() => {
        closedByUs = true;
        socket.close();
      }, holdMs);
    },
  );
  const upgraded = check(res, { 'ws 101': (r) => r && r.status === 101 });
  wsConnectSuccess.add(upgraded && ready);
  wsSessionsWithState.add(states > 0);
  return states;
}

/** The `name` tags of the requests, for the per-request table of tests/load/report.mjs. */
export const ENDPOINTS = [
  'GET /',
  'GET /_app/*',
  'GET /config.json',
  'GET /whats-new.json',
  'GET /identity/login-methods',
  'POST /identity/demo/session',
  'POST /identity/session/refresh',
  'GET /identity/me',
  'GET /identity/ui-areas',
  'GET /identity/me/platform',
  'POST /identity/me/permissions',
  'GET /entitlements/me',
  'GET /notifications/inbox',
  'GET /fleet/mowers',
  'GET /fleet/mowers/{id}',
  'GET /fleet/mowers/{id}/commands',
  'POST /fleet/mowers/{id}/commands',
  'GET /fleet/commands/{id}',
  'GET /fleet/groups',
  'GET /fleet/sites',
  'GET /telemetry/accounts/me/mowers/states',
  'GET /telemetry/accounts/me/mowers/capabilities',
  'GET /telemetry/mowers/{id}/state',
  'GET /telemetry/mowers/{id}/zones',
  'GET /telemetry/mowers/{id}/map',
  'GET /telemetry/mowers/{id}/capabilities',
  'GET /telemetry/mowers/{id}/tracking',
  'GET /telemetry/mowers/{id}/positions (today)',
  'GET /telemetry/mowers/{id}/positions (7 days)',
  'GET /telemetry/mowers/{id}/aerial',
  'GET /telemetry/mowers/{id}/health',
  'GET /telemetry/mowers/{id}/health/blade-changes',
  'GET /telemetry/mowers/{id}/health/daily',
  'GET /telemetry/mowers/{id}/battery',
  'GET /telemetry/reports/summary',
  'GET /scheduler/schedules',
  'GET /scheduler/schedules/{id}/preview',
  'GET /scheduler/schedules/{id}/runs',
  'GET /scheduler/weather/{id}',
];

/** k6 keeps a sub-metric only when a threshold names it: one that always passes per request. */
const endpointThresholds = Object.fromEntries(
  ENDPOINTS.map((name) => [`http_req_duration{name:${name}}`, ['max>=0']]),
);

/** Thresholds shared by the scenarios (abortOnFail off: a sweep wants every level's numbers). */
export const apiThresholds = {
  ...endpointThresholds,
  'http_req_duration{kind:api}': ['p(95)<500'],
  'http_req_duration{kind:static}': ['p(95)<300'],
  http_req_failed: ['rate<0.01'],
  checks: ['rate>0.99'],
};

/** Text summary on stdout and the full summary as JSON for tests/load/report.mjs. */
export function summary(data) {
  const out = { stdout: textSummary(data, { indent: ' ', enableColors: false }) };
  if (__ENV.SUMMARY_FILE) out[__ENV.SUMMARY_FILE] = JSON.stringify(data, null, 2);
  return out;
}

/** "a,b,c" -> numbers. */
export function numbers(text, fallback) {
  return String(text || fallback)
    .split(',')
    .map((v) => Number(v.trim()))
    .filter((v) => Number.isFinite(v) && v >= 0);
}

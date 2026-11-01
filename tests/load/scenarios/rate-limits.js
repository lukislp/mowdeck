// (d) The demo's per-address protection, measured on its own (docs/ops/load-testing.md): what one
// client address gets before the gateway and the services say no. The other scenarios give every
// virtual visitor its own address and never reach these limits; this one shows they are in place
// and what a single visitor (or a load test from one address) runs into.
//
//   logins   30 "Try the demo" from one address  (identity-ratelimit: 10/min, burst 20)
//   burst    900 GET /config.json from one address (demo-ratelimit: 1200/min, burst 600)
//   sockets  25 live views from one address        (20 sockets per address in the demo)
import http from 'k6/http';
import { sleep } from 'k6';
import exec from 'k6/execution';
import { Counter } from 'k6/metrics';
import ws from 'k6/ws';

import { BASE, ORIGIN, summary, Visitor } from '../lib/mowdeck.js';

const loginOk = new Counter('limit_logins_ok');
const loginLimited = new Counter('limit_logins_429');
const getOk = new Counter('limit_gets_ok');
const getLimited = new Counter('limit_gets_429');
const socketsReady = new Counter('limit_sockets_ready');
const socketsRefused = new Counter('limit_sockets_refused');

export const options = {
  scenarios: {
    logins: { executor: 'per-vu-iterations', vus: 1, iterations: 1, exec: 'logins' },
    burst: { executor: 'per-vu-iterations', vus: 1, iterations: 1, exec: 'burst' },
    sockets: {
      executor: 'per-vu-iterations',
      vus: 25,
      iterations: 1,
      exec: 'sockets',
      startTime: '5s',
    },
  },
  thresholds: {
    // The limits hold: at most the burst of logins and of sockets passes, the request burst ends.
    limit_logins_ok: ['count<=22'],
    limit_logins_429: ['count>=8'],
    limit_gets_429: ['count>=1'],
    limit_sockets_ready: ['count<=20'],
    limit_sockets_refused: ['count>=5'],
  },
};

const DEMO = { 'X-Requested-With': 'mowdeck-web', 'X-Mowdeck-Refresh': 'cookie-only' };

/** Sessions for the socket test, each from its own address (one session allows 10 sockets). */
export function setup() {
  const tokens = [];
  for (let i = 1; i <= 3; i += 1) {
    const visitor = new Visitor(`10.161.0.${i}`);
    if (visitor.signIn()) tokens.push(visitor.token);
  }
  return { tokens };
}

export function logins() {
  const visitor = new Visitor('10.160.0.1');
  for (let i = 0; i < 30; i += 1) {
    http.cookieJar().clear(BASE);
    const res = visitor.post(
      '/api/v1/identity/demo/session',
      {},
      'POST /identity/demo/session (one address)',
      DEMO,
    );
    if (res.status === 200) loginOk.add(1);
    else if (res.status === 429) loginLimited.add(1);
  }
}

export function burst() {
  const visitor = new Visitor('10.160.0.2');
  const params = {
    headers: visitor.headers(),
    tags: { name: 'GET /config.json (one address)' },
    responseCallback: http.expectedStatuses(200, 429),
  };
  for (let round = 0; round < 18; round += 1) {
    const batch = [];
    for (let i = 0; i < 50; i += 1) batch.push(['GET', `${BASE}/config.json`, null, params]);
    for (const res of http.batch(batch)) {
      if (res.status === 200) getOk.add(1);
      else if (res.status === 429) getLimited.add(1);
    }
  }
}

export function sockets(data) {
  const token = data.tokens[exec.vu.idInTest % data.tokens.length];
  let ready = false;
  ws.connect(
    `${BASE.replace(/^http/, 'ws')}/api/v1/realtime/ws`,
    {
      headers: { Origin: ORIGIN, 'X-Forwarded-For': '10.160.0.3' },
      tags: { name: 'WS (one address)' },
    },
    (socket) => {
      socket.on('open', () => socket.send(JSON.stringify({ type: 'auth', token })));
      socket.on('message', (raw) => {
        if (JSON.parse(raw).type === 'ready') ready = true;
      });
      socket.setTimeout(() => socket.close(), 15_000);
    },
  );
  if (ready) socketsReady.add(1);
  else socketsRefused.add(1);
  sleep(1);
}

export const handleSummary = summary;

// (e) Sandbox visitors (ADR 0076): every virtual user is one visitor who stays for the whole
// level. It loads the app, clicks "Try the demo" (which claims a sandbox of its own), opens the
// mower list and one mower page, starts one of its mowers and then keeps the live view (ADR 0018)
// open, reconnecting every LOAD_HOLD seconds and opening the mower page again in between, until
// the level ends. Then it logs out, so the demo deletes its sandbox within about a minute.
//
// The level is what the demo host carries: N watched sandboxes at once, each with a mower mowing,
// plus the arrival of N visitors within LOAD_RAMP. Made for the real demo host
// (tests/load/vps/ladder.sh, docs/ops/load-testing.md "Measuring the real demo host"), it runs
// against the throwaway stack of tests/load/load.mjs as well.
//
//   LOAD_VUS              visitors (sandboxes) of the level (default 10)
//   LOAD_RAMP             time in which they arrive (default 60s)
//   LOAD_DURATION         how long all of them stay after the ramp (default 5m)
//   LOAD_HOLD             seconds per live view before it reconnects (default 120, +-20 % jitter)
//   LOAD_MOW              1 (default): start one mower per sandbox; 0: only watch
//   LOAD_ADDRESS_PREFIX   first two octets of the visitors' addresses (default 198.18, the
//                         benchmark range of RFC 2544: never a real visitor)
//   LOAD_ADDRESS_OFFSET   added to the VU number (ladder.sh: one block per level), so no address
//                         comes near the demo's limit of sandboxes per address and hour
import { check, sleep } from 'k6';
import exec from 'k6/execution';
import { Counter, Trend } from 'k6/metrics';

import {
  apiThresholds,
  appShell,
  json,
  liveView,
  loadApp,
  mowerList,
  mowerPage,
  summary,
  Visitor,
  visitorAddress,
} from '../lib/mowdeck.js';

const sandboxClaimed = new Counter('sandbox_claimed');
const sandboxFull = new Counter('sandbox_full');
const sandboxRefused = new Counter('sandbox_refused');
const sandboxLost = new Counter('sandbox_lost');
const sandboxReleased = new Counter('sandbox_released');
const sandboxClaimTime = new Trend('sandbox_claim_time', true);
const mowStarted = new Counter('mow_started');

const vus = Number(__ENV.LOAD_VUS || 10);
const ramp = __ENV.LOAD_RAMP || '60s';
const duration = __ENV.LOAD_DURATION || '5m';
const hold = Number(__ENV.LOAD_HOLD || 120);
const mow = (__ENV.LOAD_MOW || '1') !== '0';
const prefix = __ENV.LOAD_ADDRESS_PREFIX || '198.18';
const offset = Number(__ENV.LOAD_ADDRESS_OFFSET || 0);

/** "90s", "5m", "1m30s", "1h" -> milliseconds. */
export function millis(text) {
  let total = 0;
  const re = /(\d+(?:\.\d+)?)(ms|s|m|h)/g;
  let m;
  while ((m = re.exec(String(text))) !== null) {
    total += Number(m[1]) * { ms: 1, s: 1000, m: 60_000, h: 3_600_000 }[m[2]];
  }
  return total;
}

export const options = {
  scenarios: {
    sandboxes: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: ramp, target: vus },
        { duration, target: vus },
      ],
      gracefulRampDown: '60s',
      gracefulStop: '60s',
    },
  },
  thresholds: {
    ...apiThresholds,
    'http_req_duration{name:POST /identity/demo/session}': ['p(95)<2000'],
    ws_connect_success: ['rate>0.99'],
    ws_sessions_with_state: ['rate>0.99'],
    ws_ready_time: ['p(95)<1000'],
  },
  summaryTrendStats: ['avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

// Per VU: the visitor, its mowers, whether it started a mower, whether it left.
let visitor = null;
let mowers = [];
let areas = [];
let mowing = false;
let left = false;

/** When every visitor of the level leaves (the end of the hold stage). */
function deadline() {
  return exec.scenario.startTime + millis(ramp) + millis(duration);
}

function startMow() {
  if (mowing || !mow || !mowers.length) return;
  const id = mowers[0];
  if (!areas.length) {
    const map = json(
      visitor.get(`/api/v1/telemetry/mowers/${id}/map`, 'GET /telemetry/mowers/{id}/map'),
    );
    areas = ((map && (map.areas || (map.map && map.map.areas))) || []).map((a) => a.id);
  }
  if (!areas.length) return;
  const res = visitor.post(
    `/api/v1/fleet/mowers/${id}/commands`,
    { command: { kind: 'start_mow', area_ids: [areas[0]] }, ttl_seconds: 30 },
    'POST /fleet/mowers/{id}/commands',
  );
  if (check(res, { 'command 202': (r) => r.status === 202 })) {
    mowing = true;
    mowStarted.add(1);
  }
}

/** Arrive: the app, "Try the demo", the mower list and one mower page. False when not let in. */
function arrive() {
  const v = new Visitor(visitorAddress(prefix, offset + exec.vu.idInTest));
  loadApp(v);
  v.get('/api/v1/identity/login-methods', 'GET /identity/login-methods');
  sleep(1 + Math.random() * 2);
  const res = v.signIn({ keepRefresh: true, expected: [200, 429, 503] });
  if (res.status === 503) {
    // The demo is full (DEMO_SANDBOX_MAX): real visitors hold the rest. Wait as the web app does.
    sandboxFull.add(1);
    sleep(30);
    return false;
  }
  if (res.status === 429) {
    sandboxRefused.add(1);
    sleep(30);
    return false;
  }
  if (!res.ok) {
    sleep(10);
    return false;
  }
  sandboxClaimed.add(1);
  sandboxClaimTime.add(res.duration);
  visitor = v;
  mowing = false;
  areas = [];
  visitor.get('/api/v1/identity/me', 'GET /identity/me');
  appShell(visitor);
  mowers = mowerList(visitor);
  sleep(2 + Math.random() * 3);
  if (mowers.length) areas = mowerPage(visitor, mowers[0]);
  return true;
}

function leave() {
  if (visitor && visitor.logout()) sandboxReleased.add(1);
  visitor = null;
  left = true;
}

export default function () {
  if (left) {
    sleep(5);
    return;
  }
  const remaining = deadline() - Date.now() - 10_000;
  if (!visitor) {
    if (remaining < 30_000 || !arrive()) {
      if (remaining < 30_000) left = true;
      return;
    }
  }
  if (!visitor.refreshIfDue()) {
    sandboxLost.add(1);
    visitor = null;
    return;
  }
  const holdMs = Math.min(
    hold * 1000 * (0.8 + Math.random() * 0.4),
    deadline() - Date.now() - 10_000,
  );
  if (holdMs < 5_000) {
    leave();
    return;
  }
  liveView(visitor, mowers, holdMs, mowing ? null : startMow);
  if (deadline() - Date.now() - 10_000 < 5_000) {
    leave();
    return;
  }
  // Between two live views the visitor opens a mower page again (a reload or a navigation).
  sleep(1 + Math.random() * 2);
  if (mowers.length) mowerPage(visitor, mowers[Math.floor(Math.random() * mowers.length)]);
}

export const handleSummary = summary;

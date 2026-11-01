// (b) Signed-in users clicking through the app: mower list, mower page, its health page,
// schedules, notifications and reports, with think time between pages and the token refresh of
// the web app. In a demo stack every user is a login session of the demo user (ADR 0068).
//
//   LOAD_USERS   concurrent users per stage (default 10,25,50), LOAD_STEP each (default 2m)
//   LOAD_THINK   seconds between two pages (default 5, +-50 % jitter)
import { check, sleep } from 'k6';
import exec from 'k6/execution';

import {
  apiThresholds,
  appShell,
  BASE,
  batch,
  items,
  json,
  mowerList,
  mowerPage,
  numbers,
  summary,
  Visitor,
  visitorAddress,
} from '../lib/mowdeck.js';

const think = Number(__ENV.LOAD_THINK || 5);

export const options = {
  scenarios: {
    users: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: numbers(__ENV.LOAD_USERS, '10,25,50').flatMap((target) => [
        { duration: '30s', target },
        { duration: __ENV.LOAD_STEP || '2m', target },
      ]),
      gracefulRampDown: '10s',
    },
  },
  thresholds: apiThresholds,
  summaryTrendStats: ['avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

const day = (offset) => new Date(Date.now() + offset * 86_400_000).toISOString().slice(0, 10);

function healthPage(user, id) {
  const now = Date.now();
  const p = (name) => user.params(name);
  batch([
    [
      'GET',
      `${BASE}/api/v1/telemetry/mowers/${id}/health`,
      null,
      p('GET /telemetry/mowers/{id}/health'),
    ],
    [
      'GET',
      `${BASE}/api/v1/telemetry/mowers/${id}/health/blade-changes`,
      null,
      p('GET /telemetry/mowers/{id}/health/blade-changes'),
    ],
    ['GET', `${BASE}/api/v1/fleet/mowers/${id}`, null, p('GET /fleet/mowers/{id}')],
    [
      'GET',
      `${BASE}/api/v1/telemetry/mowers/${id}/health/daily?from=${day(-29)}&to=${day(0)}`,
      null,
      p('GET /telemetry/mowers/{id}/health/daily'),
    ],
    [
      'GET',
      `${BASE}/api/v1/telemetry/mowers/${id}/battery?from=${new Date(now - 86_400_000).toISOString()}&to=${new Date(now).toISOString()}&step=15m`,
      null,
      p('GET /telemetry/mowers/{id}/battery'),
    ],
  ]);
}

function schedulesPage(user) {
  const res = batch([
    ['GET', `${BASE}/api/v1/scheduler/schedules`, null, user.params('GET /scheduler/schedules')],
    ['GET', `${BASE}/api/v1/fleet/mowers`, null, user.params('GET /fleet/mowers')],
    ['GET', `${BASE}/api/v1/fleet/groups`, null, user.params('GET /fleet/groups')],
    [
      'GET',
      `${BASE}/api/v1/fleet/sites?include_default=true`,
      null,
      user.params('GET /fleet/sites'),
    ],
  ]);
  check(res[0], { 'schedules 200': (r) => r.status === 200 });
  const schedules = items(json(res[0]), 'schedules');
  const per = schedules.slice(0, 10).flatMap((s) => [
    [
      'GET',
      `${BASE}/api/v1/scheduler/schedules/${s.id}/preview?count=3`,
      null,
      user.params('GET /scheduler/schedules/{id}/preview'),
    ],
    [
      'GET',
      `${BASE}/api/v1/scheduler/schedules/${s.id}/runs?limit=1`,
      null,
      user.params('GET /scheduler/schedules/{id}/runs'),
    ],
  ]);
  if (per.length) batch(per);
}

function notificationsPage(user) {
  const res = batch([
    [
      'GET',
      `${BASE}/api/v1/notifications/inbox?limit=50`,
      null,
      user.params('GET /notifications/inbox'),
    ],
    ['GET', `${BASE}/api/v1/fleet/mowers`, null, user.params('GET /fleet/mowers')],
  ]);
  check(res[0], { 'inbox 200': (r) => r.status === 200 });
}

function reportsPage(user) {
  const res = batch([
    ['GET', `${BASE}/api/v1/fleet/mowers`, null, user.params('GET /fleet/mowers')],
    ['GET', `${BASE}/api/v1/fleet/groups`, null, user.params('GET /fleet/groups')],
    [
      'GET',
      `${BASE}/api/v1/fleet/sites?include_default=true`,
      null,
      user.params('GET /fleet/sites'),
    ],
    [
      'GET',
      `${BASE}/api/v1/telemetry/reports/summary?from=${day(-6)}&to=${day(0)}`,
      null,
      user.params('GET /telemetry/reports/summary'),
    ],
  ]);
  check(res[3], { 'report 200': (r) => r.status === 200 });
}

let user = null;
let ids = [];

export default function () {
  if (!user) {
    user = new Visitor(visitorAddress('10.120', exec.vu.idInTest));
    if (!user.signIn()) {
      user = null;
      sleep(10);
      return;
    }
    appShell(user);
  }
  user.refreshIfDue();
  const page = Math.random();
  if (page < 0.25 || !ids.length) ids = mowerList(user);
  else if (page < 0.5) mowerPage(user, ids[Math.floor(Math.random() * ids.length)]);
  else if (page < 0.6) healthPage(user, ids[Math.floor(Math.random() * ids.length)]);
  else if (page < 0.75) schedulesPage(user);
  else if (page < 0.9) notificationsPage(user);
  else reportsPage(user);
  sleep(think * (0.5 + Math.random()));
}

export const handleSummary = summary;

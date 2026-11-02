// (a) Demo visitors (ADR 0068): load the app with an empty cache, "Try the demo", the mower list,
// one mower page, then hold the live view (ADR 0018) for minutes while telemetry arrives. Every
// virtual user is one concurrent visitor with its own client address.
//
//   LOAD_MODE=ramp   (default) ramp through LOAD_TARGETS, LOAD_STEP each (one run, CI)
//   LOAD_MODE=level  ramp to LOAD_VUS over LOAD_RAMP, hold LOAD_DURATION (one level of a sweep)
//   LOAD_HOLD        seconds a visitor keeps the live view open (default 120, +-20 % jitter)
import { sleep } from 'k6';
import exec from 'k6/execution';

import {
  apiThresholds,
  appShell,
  liveView,
  loadApp,
  mowerList,
  mowerPage,
  numbers,
  summary,
  Visitor,
  visitorAddress,
} from '../lib/mowdeck.js';

const mode = __ENV.LOAD_MODE || 'ramp';
const hold = Number(__ENV.LOAD_HOLD || 120);

function stages() {
  if (mode === 'level') {
    return [
      { duration: __ENV.LOAD_RAMP || '60s', target: Number(__ENV.LOAD_VUS || 50) },
      { duration: __ENV.LOAD_DURATION || '4m', target: Number(__ENV.LOAD_VUS || 50) },
    ];
  }
  const step = __ENV.LOAD_STEP || '2m';
  return numbers(__ENV.LOAD_TARGETS, '10,25,50').flatMap((target) => [
    { duration: '30s', target },
    { duration: step, target },
  ]);
}

export const options = {
  scenarios: {
    visitors: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: stages(),
      gracefulRampDown: '10s',
      gracefulStop: '30s',
    },
  },
  thresholds: {
    ...apiThresholds,
    ws_connect_success: ['rate>0.99'],
    ws_sessions_with_state: ['rate>0.99'],
    ws_ready_time: ['p(95)<1000'],
  },
  summaryTrendStats: ['avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

export default function () {
  // A new visitor per iteration, but a VU keeps its address (one visitor after another).
  const visitor = new Visitor(visitorAddress('10.100', exec.vu.idInTest));
  loadApp(visitor);
  visitor.get('/api/v1/identity/login-methods', 'GET /identity/login-methods');
  sleep(1 + Math.random() * 2);
  if (!visitor.signIn()) {
    sleep(10);
    return;
  }
  visitor.get('/api/v1/identity/me', 'GET /identity/me');
  appShell(visitor);
  const ids = mowerList(visitor);
  sleep(2 + Math.random() * 3);
  if (ids.length) mowerPage(visitor, ids[Math.floor(Math.random() * ids.length)]);
  const holdMs = hold * 1000 * (0.8 + Math.random() * 0.4);
  liveView(visitor, ids, holdMs);
}

export const handleSummary = summary;

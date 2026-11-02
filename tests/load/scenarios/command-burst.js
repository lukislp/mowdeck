// (c) A burst of commands (start, pause, resume, dock) against the demo's simulated mowers, at a
// rising arrival rate. Each command is followed to its final status the way the web app falls
// back when the socket is quiet (GET /fleet/commands/{id}), which measures the whole path
// Fleet -> NATS -> Device-Gateway -> simulator -> result -> Fleet.
//
//   LOAD_RATES    commands per second per stage (default 1,5,10,20), LOAD_STEP each (default 1m)
//   LOAD_SENDERS  distinct client addresses that send (default 20). Every sender stays far below
//                 the demo's per-address limit (1200 requests a minute).
import { check, sleep } from 'k6';
import exec from 'k6/execution';
import { Counter, Trend } from 'k6/metrics';

import {
  apiThresholds,
  json,
  mowerList,
  mowerPage,
  numbers,
  summary,
  Visitor,
  visitorAddress,
} from '../lib/mowdeck.js';

const commandFinalTime = new Trend('command_final_time', true);
const commandStatus = new Counter('command_status');
const senders = Number(__ENV.LOAD_SENDERS || 20);
const FINAL = new Set(['executed', 'rejected', 'failed', 'expired']);

export const options = {
  scenarios: {
    commands: {
      executor: 'ramping-arrival-rate',
      startRate: 0,
      timeUnit: '1s',
      preAllocatedVUs: 50,
      maxVUs: Number(__ENV.LOAD_MAX_VUS || 400),
      stages: numbers(__ENV.LOAD_RATES, '1,5,10,20').flatMap((target) => [
        { duration: '10s', target },
        { duration: __ENV.LOAD_STEP || '1m', target },
      ]),
    },
  },
  thresholds: {
    ...apiThresholds,
    'http_req_duration{name:POST /fleet/mowers/{id}/commands}': ['p(95)<500'],
    command_final_time: ['p(95)<5000'],
  },
  summaryTrendStats: ['avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

let sender = null;
let mowers = [];
const areas = {};

function command(id) {
  const roll = Math.random();
  if (roll < 0.35 && areas[id] && areas[id].length) {
    return {
      kind: 'start_mow',
      area_ids: [areas[id][Math.floor(Math.random() * areas[id].length)]],
    };
  }
  if (roll < 0.6) return { kind: 'pause_mow' };
  if (roll < 0.8) return { kind: 'resume_mow' };
  return { kind: 'return_to_dock' };
}

export default function () {
  if (!sender) {
    sender = new Visitor(visitorAddress('10.140', exec.vu.idInTest % senders || senders));
    if (!sender.signIn()) {
      sender = null;
      return;
    }
    mowers = mowerList(sender);
    for (const id of mowers) areas[id] = mowerPage(sender, id);
  }
  sender.refreshIfDue();
  if (!mowers.length) return;
  const id = mowers[Math.floor(Math.random() * mowers.length)];
  const body = { command: command(id), ttl_seconds: 30 };
  const started = Date.now();
  const res = sender.post(
    `/api/v1/fleet/mowers/${id}/commands`,
    body,
    'POST /fleet/mowers/{id}/commands',
  );
  if (!check(res, { 'command 202': (r) => r.status === 202 })) return;
  const commandId = (json(res) || {}).command_id;
  let status = 'accepted';
  while (!FINAL.has(status) && Date.now() - started < 35_000) {
    sleep(0.5);
    const poll = sender.get(`/api/v1/fleet/commands/${commandId}`, 'GET /fleet/commands/{id}');
    status = (json(poll) || {}).status || status;
  }
  commandStatus.add(1, { status, kind: body.command.kind });
  if (FINAL.has(status)) commandFinalTime.add(Date.now() - started, { kind: body.command.kind });
}

export const handleSummary = summary;

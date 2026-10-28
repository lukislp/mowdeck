import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  planIntent,
  snapshotFromWidget,
  startCommand,
  type Activity,
  type Intent,
  type MowerSnapshot,
} from '../src/model.ts';

const at = (activity: Activity): MowerSnapshot => ({ activity, batteryPercent: 50, charging: false });

describe('snapshotFromWidget', () => {
  it('reads activity, battery and charging', () => {
    assert.deepEqual(
      snapshotFromWidget({ activity: 'mowing', battery_percent: 63.6, charging: false, stale: false }),
      { activity: 'mowing', batteryPercent: 64, charging: false },
    );
  });

  it('shows a stale state as offline and unknown activities as unknown', () => {
    assert.equal(snapshotFromWidget({ activity: 'mowing', stale: true }).activity, 'offline');
    assert.equal(snapshotFromWidget({ activity: 'teleporting' }).activity, 'unknown');
    assert.deepEqual(snapshotFromWidget(null), {
      activity: 'unknown',
      batteryPercent: null,
      charging: false,
    });
  });

  it('clamps the battery', () => {
    assert.equal(snapshotFromWidget({ activity: 'idle', battery_percent: 140 }).batteryPercent, 100);
    assert.equal(snapshotFromWidget({ activity: 'idle', battery_percent: -3 }).batteryPercent, 0);
    assert.equal(snapshotFromWidget({ activity: 'idle', battery_percent: 'x' }).batteryPercent, null);
  });
});

describe('planIntent', () => {
  const cases: [Intent, Activity, unknown][] = [
    ['start', 'docked', { kind: 'start' }],
    ['start', 'charging', { kind: 'start' }],
    ['start', 'idle', { kind: 'start' }],
    ['start', 'paused', { kind: 'command', command: { kind: 'resume_mow' } }],
    ['start', 'mowing', { kind: 'noop' }],
    ['start', 'error', { kind: 'refuse', reason: 'error' }],
    ['pause', 'mowing', { kind: 'command', command: { kind: 'pause_mow' } }],
    ['pause', 'paused', { kind: 'noop' }],
    ['pause', 'docked', { kind: 'refuse', reason: 'invalid_in_state' }],
    ['resume', 'paused', { kind: 'command', command: { kind: 'resume_mow' } }],
    ['resume', 'docked', { kind: 'refuse', reason: 'invalid_in_state' }],
    ['dock', 'mowing', { kind: 'command', command: { kind: 'return_to_dock' } }],
    ['dock', 'paused', { kind: 'command', command: { kind: 'return_to_dock' } }],
    ['dock', 'error', { kind: 'command', command: { kind: 'return_to_dock' } }],
    ['dock', 'docked', { kind: 'noop' }],
    ['dock', 'returning', { kind: 'noop' }],
    ['stop', 'mowing', { kind: 'command', command: { kind: 'return_to_dock', end_job: true } }],
    ['stop', 'paused', { kind: 'command', command: { kind: 'return_to_dock', end_job: true } }],
    ['stop', 'charging', { kind: 'noop' }],
  ];
  for (const [intent, activity, expected] of cases) {
    it(`${intent} while ${activity}`, () => {
      assert.deepEqual(planIntent(intent, at(activity)), expected);
    });
  }

  it('refuses everything while offline, unknown or updating', () => {
    for (const intent of ['start', 'stop', 'pause', 'resume', 'dock'] as Intent[]) {
      assert.deepEqual(planIntent(intent, at('offline')), { kind: 'refuse', reason: 'offline' });
      assert.deepEqual(planIntent(intent, at('unknown')), { kind: 'refuse', reason: 'offline' });
      assert.deepEqual(planIntent(intent, at('updating')), { kind: 'refuse', reason: 'updating' });
    }
  });

  it('never plans an emergency stop or a command with blades kept running elsewhere', () => {
    const activities: Activity[] = ['idle', 'mowing', 'paused', 'returning', 'docked', 'charging'];
    for (const activity of activities) {
      for (const intent of ['start', 'stop', 'pause', 'resume', 'dock'] as Intent[]) {
        const plan = planIntent(intent, at(activity));
        if (plan.kind === 'command') {
          assert.ok(
            ['resume_mow', 'pause_mow', 'return_to_dock'].includes(String(plan.command.kind)),
            `${intent}/${activity}: ${JSON.stringify(plan.command)}`,
          );
        }
      }
    }
  });
});

describe('startCommand', () => {
  it('starts every area with the saved pattern, or without one', () => {
    assert.deepEqual(startCommand(['a', 'b'], { kind: 'stripes', angle: 30 }), {
      kind: 'start_mow',
      area_ids: ['a', 'b'],
      pattern: { kind: 'stripes', angle: 30 },
    });
    assert.deepEqual(startCommand(['a'], null), { kind: 'start_mow', area_ids: ['a'] });
  });
});

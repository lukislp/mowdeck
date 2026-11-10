// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { flushSync } from 'svelte';
import { describe, expect, it } from 'vitest';

import { SafetyGate } from './gate.svelte';

// A `.svelte.test.ts` file so the rune-based state is available.

describe('SafetyGate', () => {
  it('answers true at once, without a sheet, for an action that needs none', async () => {
    const gate = new SafetyGate();
    expect(await gate.ask('pause_mow')).toBe(true);
    expect(await gate.ask('emergency_stop')).toBe(true);
    expect(await gate.ask('return_to_dock')).toBe(true);
    expect(gate.open).toBe(false);
  });

  it('opens for a start and resolves with the user answer', async () => {
    const gate = new SafetyGate();
    const answer = gate.ask('start_mow');
    flushSync();
    expect(gate.open).toBe(true);
    expect(gate.variant).toBe('now');
    gate.confirm();
    expect(await answer).toBe(true);
    expect(gate.open).toBe(false);
  });

  it('resolves false when cancelled', async () => {
    const gate = new SafetyGate();
    const answer = gate.ask('drive_session');
    gate.cancel();
    expect(await answer).toBe(false);
    expect(gate.open).toBe(false);
  });

  it('asks again for the next start: nothing is remembered', async () => {
    const gate = new SafetyGate();
    const first = gate.ask('start_mow');
    gate.confirm();
    await first;
    const second = gate.ask('start_mow');
    expect(gate.open).toBe(true);
    gate.cancel();
    expect(await second).toBe(false);
  });

  it('uses the unattended wording for a schedule', () => {
    const gate = new SafetyGate();
    void gate.ask('schedule_enable');
    expect(gate.variant).toBe('unattended');
    gate.cancel();
  });

  it('shares the one sheet between two simultaneous questions', async () => {
    const gate = new SafetyGate();
    const a = gate.ask('start_mow');
    const b = gate.ask('resume_mow');
    expect(b).toBe(a);
    gate.confirm();
    expect(await Promise.all([a, b])).toEqual([true, true]);
  });

  it('does not open for a flow already covered by its own confirmation', async () => {
    const gate = new SafetyGate();
    expect(await gate.ask('spot_mow', true)).toBe(true);
    expect(gate.open).toBe(false);
  });
});

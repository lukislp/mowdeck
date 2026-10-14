import { flushSync } from 'svelte';
import { describe, expect, it, vi } from 'vitest';

import { StartSheetFlow, type StartSheetContext, type StartSheetDeps } from './start-sheet.svelte';

// Runes in a test: the mower page resets the sheet from an `$effect` that runs per mower; this
// file is a `.svelte.test.ts` so `$effect.root` is available.

function deps(): StartSheetDeps {
  const context: StartSheetContext = {
    areas: [],
    offered: ['stripes', 'diagonal'],
    spacing: null,
    savedPattern: null,
    bladeRange: [25, 70],
    speedRange: [0.2, 0.6],
    reportedBladeMm: 50,
    reportedSpeedMps: 0.4,
    cuttingWidthMm: 400,
    rain: null,
  };
  return {
    preview: vi.fn(),
    setpoint: vi.fn(async () => true),
    start: vi.fn(async () => true),
    dock: vi.fn(async () => true),
    saveDefaults: vi.fn(async () => undefined),
    paths: { replace: vi.fn(), ended: vi.fn() },
    now: () => 0,
    context: () => context,
  };
}

describe('start sheet flow in effects', () => {
  it('a reset from an effect does not subscribe that effect to the selection', () => {
    const flow = new StartSheetFlow(deps());
    let runs = 0;
    const stop = $effect.root(() => {
      $effect(() => {
        runs += 1;
        flow.reset();
      });
    });
    flushSync();
    expect(runs).toBe(1);
    flow.open();
    flow.togglePick('a');
    flushSync();
    // Picking must stick: the page's per-mower effect ran once and did not reset the pick.
    expect(flow.picked).toEqual(['a']);
    expect(runs).toBe(1);
    flow.patternDraft = { ...flow.patternDraft, name: 'diagonal' };
    flushSync();
    expect(flow.picked).toEqual(['a']);
    expect(runs).toBe(1);
    stop();
  });
});

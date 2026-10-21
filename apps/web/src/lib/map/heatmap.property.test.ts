/**
 * Property-based tests (fast-check) of the heat layer helpers: the period window built from the
 * command list Fleet returns, the request path and the guards of stored preferences.
 */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import {
  HEAT_LAYERS,
  HEAT_PERIODS,
  heatmapUrl,
  isHeatCellSize,
  isHeatLayer,
  isHeatPeriod,
  periodWindow,
  type CommandLike,
} from './heatmap';

const MIN = Date.UTC(2020, 0, 1);
const MAX = Date.UTC(2030, 0, 1);
const instant = fc.integer({ min: MIN, max: MAX }).map((ms) => new Date(ms));
const command: fc.Arbitrary<CommandLike> = fc.record({
  kind: fc.constantFrom('start_mow', 'return_to_dock', 'pause_mow', 'set_speed'),
  issued_at: fc.oneof(
    instant.map((d) => d.toISOString()),
    fc.string(),
  ),
});

describe('heat layer properties', () => {
  it('builds a window that never ends before it starts', () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...HEAT_PERIODS),
        instant,
        fc.array(command, { maxLength: 12 }),
        (period, now, commands) => {
          const window = periodWindow(period, now, commands);
          const from = Date.parse(window.from);
          const to = Date.parse(window.to);
          expect(Number.isNaN(from)).toBe(false);
          expect(Number.isNaN(to)).toBe(false);
          expect(from).toBeLessThanOrEqual(to);
        },
      ),
      { numRuns: 1000 },
    );
  });

  it('keeps the mower id inside its path segment', () => {
    fc.assert(
      fc.property(fc.string(), instant, fc.constantFrom(1, 2, 5), (mowerId, now, cell) => {
        const url = new URL(
          heatmapUrl(mowerId, 'coverage', periodWindow('24h', now), cell),
          'https://mowdeck.example',
        );
        const segments = url.pathname.split('/');
        expect(segments.slice(0, 5)).toEqual(['', 'api', 'v1', 'telemetry', 'mowers']);
        expect(segments).toHaveLength(7);
        expect(decodeURIComponent(segments[5] ?? '')).toBe(mowerId);
        expect(url.searchParams.get('layer')).toBe('coverage');
      }),
    );
  });

  it('accepts only the known layers, periods and cell sizes', () => {
    fc.assert(
      fc.property(fc.anything(), (value) => {
        expect(isHeatLayer(value)).toBe(
          typeof value === 'string' && (HEAT_LAYERS as readonly string[]).includes(value),
        );
        expect(isHeatPeriod(value)).toBe(
          typeof value === 'string' && (HEAT_PERIODS as readonly string[]).includes(value),
        );
        if (isHeatCellSize(value)) expect([1, 2, 5]).toContain(Number(value));
      }),
    );
  });
});

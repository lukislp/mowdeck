/**
 * Property-based test (fast-check) of the reconnect backoff: full jitter, never negative,
 * never above the cap, whatever the attempt count and the random source return.
 */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { BACKOFF_BASE_MS, BACKOFF_MAX_MS, backoffDelay } from './client.svelte';

describe('reconnect backoff properties', () => {
  it('stays an integer between zero and the capped exponential bound', () => {
    fc.assert(
      fc.property(
        fc.nat({ max: 10_000 }),
        fc.double({ min: 0, max: 1, maxExcluded: true, noNaN: true }),
        (attempt, random) => {
          const delay = backoffDelay(attempt, () => random);
          expect(Number.isInteger(delay)).toBe(true);
          expect(delay).toBeGreaterThanOrEqual(0);
          expect(delay).toBeLessThanOrEqual(
            Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** attempt),
          );
        },
      ),
    );
  });
});

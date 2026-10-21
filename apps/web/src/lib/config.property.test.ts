/**
 * Property-based tests (fast-check) of the runtime config: `/config.json` is written by the
 * operator's deployment and parsed before anything else, so no value may break the app and
 * only well-formed values may come through.
 */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { DEFAULT_CONFIG, parseConfig } from './config';
import { DEFAULT_SOURCE, OFFICIAL_REPOSITORY, parseSource, sourceCodeUrl } from './legal/source';

const tileUrl = fc
  .tuple(fc.webUrl(), fc.constantFrom('/{z}/{x}/{y}.png', '/{x}/{y}', '?z={z}&x={x}&y={y}', ''))
  .map(([base, path]) => base + path);

const config = fc.record(
  {
    mapTiles: fc.oneof(
      fc.anything(),
      fc.record(
        {
          url: fc.oneof(tileUrl, fc.string()),
          attribution: fc.oneof(fc.string(), fc.anything()),
          maxZoom: fc.oneof(fc.integer({ min: -5, max: 30 }), fc.double(), fc.anything()),
        },
        { requiredKeys: [] },
      ),
    ),
    legalEnabled: fc.anything(),
    cameraEnabled: fc.anything(),
    source: fc.oneof(
      fc.anything(),
      fc.record(
        { url: fc.oneof(fc.webUrl(), fc.string()), version: fc.string(), ref: fc.string() },
        { requiredKeys: [] },
      ),
    ),
  },
  { requiredKeys: [] },
);

describe('runtime config properties', () => {
  it('never throws and lets only well-formed values through', () => {
    fc.assert(
      fc.property(fc.oneof(config, fc.anything(), fc.jsonValue()), (raw) => {
        const parsed = parseConfig(raw);
        expect(typeof parsed.legalEnabled).toBe('boolean');
        expect(typeof parsed.cameraEnabled).toBe('boolean');
        const tiles = parsed.mapTiles;
        if (tiles !== null) {
          expect(tiles.url).toMatch(/^https?:\/\/[^\s"'<>]+$/);
          for (const placeholder of ['{z}', '{x}', '{y}']) expect(tiles.url).toContain(placeholder);
          expect(Number.isInteger(tiles.maxZoom)).toBe(true);
          expect(tiles.maxZoom).toBeGreaterThanOrEqual(1);
          expect(tiles.maxZoom).toBeLessThanOrEqual(22);
        }
      }),
      { numRuns: 1000 },
    );
  });

  it('falls back to the defaults for anything that is not an object', () => {
    fc.assert(
      fc.property(fc.oneof(fc.string(), fc.double(), fc.boolean(), fc.constant(null)), (raw) => {
        expect(parseConfig(raw)).toBe(DEFAULT_CONFIG);
      }),
    );
  });

  it('links the source code only to an http(s) repository', () => {
    fc.assert(
      fc.property(config, (raw) => {
        const source = parseSource((raw as { source?: unknown }).source);
        const url = new URL(sourceCodeUrl(source));
        expect(['http:', 'https:']).toContain(url.protocol);
        if (source === DEFAULT_SOURCE) expect(source.repository).toBe(OFFICIAL_REPOSITORY);
      }),
      { numRuns: 1000 },
    );
  });
});

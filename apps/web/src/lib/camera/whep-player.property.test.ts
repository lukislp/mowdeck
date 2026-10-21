/**
 * Property-based tests (fast-check) of the WHEP session URL: the relay answers an offer with a
 * `Location` the player later DELETEs, so a root-relative one must stay on the page's origin and
 * get back the path prefix of a relay behind a reverse proxy.
 */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { resolveLocation } from './whep-player';

const ORIGIN = 'https://mowdeck.example';
const segment = fc.stringMatching(/^[A-Za-z0-9_-]{1,12}$/);
const path = fc.array(segment, { minLength: 1, maxLength: 5 });

describe('WHEP Location properties', () => {
  it('keeps a root-relative Location on the page origin', () => {
    fc.assert(
      fc.property(path, path, (endpoint, location) => {
        const url = new URL(
          resolveLocation(`/${location.join('/')}`, `/${endpoint.join('/')}`, ORIGIN),
        );
        expect(url.origin).toBe(ORIGIN);
      }),
    );
  });

  it('restores the proxy prefix when the Location repeats the end of the endpoint', () => {
    fc.assert(
      fc.property(path, path, segment, (prefix, relayPath, session) => {
        const endpoint = `/${[...prefix, ...relayPath].join('/')}`;
        const location = `/${[...relayPath, session].join('/')}`;
        const resolved = new URL(resolveLocation(location, endpoint, ORIGIN));
        expect(resolved.origin).toBe(ORIGIN);
        expect(resolved.pathname.endsWith(location)).toBe(true);
        expect(resolved.pathname.startsWith(`/${prefix.join('/')}/`)).toBe(true);
      }),
    );
  });

  it('keeps an absolute Location as it is', () => {
    fc.assert(
      fc.property(fc.webUrl({ withQueryParameters: true }), path, (location, endpoint) => {
        expect(resolveLocation(location, `/${endpoint.join('/')}`, ORIGIN)).toBe(
          new URL(location).toString(),
        );
      }),
    );
  });
});

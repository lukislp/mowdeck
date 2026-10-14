import { describe, expect, it } from 'vitest';

import { isMowDeckManifest, normalizeServerUrl } from './server-url';

describe('normalizeServerUrl', () => {
  it.each([
    ['mowdeck.example.lan', 'https://mowdeck.example.lan'],
    ['  https://MowDeck.Example.lan/  ', 'https://mowdeck.example.lan'],
    ['https://mowdeck.example.lan:8443', 'https://mowdeck.example.lan:8443'],
    ['192.168.1.20', 'https://192.168.1.20'],
  ])('accepts %s as %s', (input, origin) => {
    expect(normalizeServerUrl(input)).toEqual({ ok: true, origin, insecure: false });
  });

  it('flags plain HTTP except on localhost', () => {
    expect(normalizeServerUrl('http://mowdeck.lan')).toEqual({
      ok: true,
      origin: 'http://mowdeck.lan',
      insecure: true,
    });
    expect(normalizeServerUrl('http://localhost:5173')).toEqual({
      ok: true,
      origin: 'http://localhost:5173',
      insecure: false,
    });
  });

  it.each([
    ['', 'empty'],
    ['   ', 'empty'],
    ['ftp://mowdeck.lan', 'scheme'],
    ['mowdeck://mowers', 'scheme'],
    ['https://mowdeck.lan/app', 'path'],
    ['https://mowdeck.lan/?x=1', 'path'],
    ['https://mowdeck.lan/#top', 'path'],
    ['https://user:pw@mowdeck.lan', 'credentials'],
    ['https://', 'invalid'],
    ['http://exa mple.lan', 'invalid'],
  ])('rejects %j (%s)', (input, problem) => {
    expect(normalizeServerUrl(input)).toEqual({ ok: false, problem });
  });
});

describe('isMowDeckManifest', () => {
  it('recognises the manifest as text or object', () => {
    expect(isMowDeckManifest('{"name":"MowDeck"}')).toBe(true);
    expect(isMowDeckManifest({ short_name: 'MowDeck' })).toBe(true);
  });

  it('rejects anything else', () => {
    expect(isMowDeckManifest('<html>')).toBe(false);
    expect(isMowDeckManifest({ name: 'Other' })).toBe(false);
    expect(isMowDeckManifest(null)).toBe(false);
  });
});

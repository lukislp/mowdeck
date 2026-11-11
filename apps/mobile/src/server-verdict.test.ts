// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { describe, expect, it } from 'vitest';

import { decideServer, type ProbeResult } from './server-verdict';

const https = { origin: 'https://mowdeck.example.org', insecure: false };
const http = { origin: 'http://mowdeck.lan', insecure: true };
const manifest = '{"name":"MowDeck","short_name":"MowDeck"}';

type Response = Extract<ProbeResult, { kind: 'response' }>;
const ok = (extra: Partial<Response> = {}): ProbeResult => ({
  kind: 'response',
  status: 200,
  data: manifest,
  ...extra,
});
const answer = (status: number, extra: Partial<Response> = {}): ProbeResult => ({
  kind: 'response',
  status,
  data: '',
  ...extra,
});

describe('decideServer', () => {
  it('connects to a server that gives the MowDeck signal', () => {
    expect(decideServer(https, ok())).toEqual({
      kind: 'connect',
      origin: 'https://mowdeck.example.org',
      insecure: false,
    });
    const same = ok({ url: 'https://mowdeck.example.org/manifest.webmanifest' });
    expect(decideServer(https, same)).toMatchObject({ kind: 'connect' });
  });

  it('keeps the insecure flag of plain http, except on localhost', () => {
    expect(decideServer(http, ok())).toMatchObject({ kind: 'connect', insecure: true });
    const local = { origin: 'http://localhost:8080', insecure: false };
    expect(decideServer(local, ok())).toMatchObject({ kind: 'connect', insecure: false });
  });

  it.each([
    ['another website', ok({ data: '{"name":"Router"}' })],
    ['an HTML page with 200', ok({ data: '<html><title>Sign in</title></html>' })],
    ['a 404', answer(404)],
    ['a 401 of a login portal', answer(401, { data: '<html>' })],
    ['a 403', answer(403)],
    ['a 500 from the site itself', answer(500)],
    ['a captive portal redirect', answer(302)],
    ['a 200 without a body', ok({ data: undefined })],
  ])('refuses %s without a way around', (_, probe) => {
    expect(decideServer(https, probe)).toEqual({ kind: 'not-mowdeck', origin: https.origin });
  });

  it.each([
    ['a failed request', { kind: 'failed' } as ProbeResult],
    ['a 502', answer(502)],
    ['a 503', answer(503)],
    ['a 504', answer(504)],
    ['a CDN origin-down 522', answer(522)],
  ])('offers "use anyway" for %s', (_, probe) => {
    expect(decideServer(http, probe)).toEqual({
      kind: 'unreachable',
      origin: 'http://mowdeck.lan',
      insecure: true,
    });
  });

  it('accepts a redirect to another host only when that host gives the signal', () => {
    const moved = 'https://other.example.net/manifest.webmanifest';
    expect(decideServer(https, ok({ url: moved }))).toEqual({
      kind: 'redirected',
      origin: 'https://other.example.net',
      insecure: false,
      host: 'other.example.net',
    });
    expect(decideServer(https, ok({ url: moved, data: '<html>' }))).toMatchObject({
      kind: 'not-mowdeck',
    });
  });

  it('names host and port of the new host, and flags an insecure one', () => {
    const moved = ok({ url: 'http://nas.lan:8080/manifest.webmanifest' });
    expect(decideServer(http, moved)).toMatchObject({
      kind: 'redirected',
      host: 'nas.lan:8080',
      insecure: true,
    });
  });

  it('follows a change of scheme or port on the same host without asking', () => {
    const upgraded = ok({ url: 'https://mowdeck.lan/manifest.webmanifest' });
    expect(decideServer(http, upgraded)).toEqual({
      kind: 'connect',
      origin: 'https://mowdeck.lan',
      insecure: false,
    });
    const port = ok({ url: 'https://MowDeck.example.org:8443/manifest.webmanifest' });
    expect(decideServer(https, port)).toMatchObject({
      kind: 'connect',
      origin: 'https://mowdeck.example.org:8443',
    });
  });

  it('refuses a downgrade from https to http and an unreadable final address', () => {
    const down = ok({ url: 'http://mowdeck.example.org/manifest.webmanifest' });
    expect(decideServer(https, down)).toMatchObject({ kind: 'not-mowdeck' });
    expect(decideServer(https, ok({ url: 'not a url' }))).toMatchObject({ kind: 'not-mowdeck' });
    expect(decideServer(https, ok({ url: 'ftp://mowdeck.example.org/x' }))).toMatchObject({
      kind: 'not-mowdeck',
    });
  });

  it('asks to probe https once when http redirects to it on the same host', () => {
    const redirect = answer(301, { location: 'https://mowdeck.lan/manifest.webmanifest' });
    expect(decideServer(http, redirect)).toEqual({
      kind: 'upgrade',
      origin: 'https://mowdeck.lan',
    });
    for (const location of [
      'https://evil.example/',
      'http://mowdeck.lan/x',
      undefined,
      'http://',
    ]) {
      expect(decideServer(http, answer(302, { location }))).toMatchObject({ kind: 'not-mowdeck' });
    }
    // an https address never "upgrades"
    const same = answer(301, { location: 'https://mowdeck.example.org/x' });
    expect(decideServer(https, same)).toMatchObject({ kind: 'not-mowdeck' });
  });
});

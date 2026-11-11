// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { describe, expect, it, vi } from 'vitest';

import { checkServer } from './connect';

describe('checkServer', () => {
  it('reports an invalid address without a request', async () => {
    const get = vi.fn();
    expect(await checkServer('https://mowdeck.lan/x', get)).toEqual({
      kind: 'invalid',
      problem: 'path',
    });
    expect(get).not.toHaveBeenCalled();
  });

  it('accepts a server that serves the MowDeck manifest', async () => {
    const get = vi.fn().mockResolvedValue({ status: 200, data: '{"name":"MowDeck"}' });
    expect(await checkServer('mowdeck.lan', get)).toEqual({
      kind: 'connect',
      origin: 'https://mowdeck.lan',
      insecure: false,
    });
    expect(get).toHaveBeenCalledWith('https://mowdeck.lan/manifest.webmanifest');
  });

  it('refuses a site that answers without the signal, whatever it answers', async () => {
    const other = vi.fn().mockResolvedValue({ status: 200, data: { name: 'Router' } });
    const missing = vi.fn().mockResolvedValue({ status: 404, data: '' });
    for (const get of [other, missing]) {
      expect(await checkServer('http://mowdeck.lan', get)).toEqual({
        kind: 'not-mowdeck',
        origin: 'http://mowdeck.lan',
      });
    }
  });

  it('reports a failed request or a proxy error as unreachable, with the address to confirm', async () => {
    const failed = vi.fn().mockRejectedValue(new Error('TLS'));
    const proxy = vi.fn().mockResolvedValue({ status: 503, data: '' });
    for (const get of [failed, proxy]) {
      expect(await checkServer('http://mowdeck.lan', get)).toEqual({
        kind: 'unreachable',
        origin: 'http://mowdeck.lan',
        insecure: true,
      });
    }
  });

  it('asks to confirm the new host when the address redirects to a MowDeck server', async () => {
    const get = vi.fn().mockResolvedValue({
      status: 200,
      data: '{"name":"MowDeck"}',
      url: 'https://mowdeck.example.net/manifest.webmanifest',
    });
    expect(await checkServer('mowdeck.example.org', get)).toEqual({
      kind: 'redirected',
      origin: 'https://mowdeck.example.net',
      insecure: false,
      host: 'mowdeck.example.net',
    });
  });

  it('probes https once when http:// redirects to it', async () => {
    const get = vi
      .fn()
      .mockResolvedValueOnce({ status: 301, data: '', location: 'https://mowdeck.lan/' })
      .mockResolvedValueOnce({ status: 200, data: '{"name":"MowDeck"}' });
    expect(await checkServer('http://mowdeck.lan', get)).toEqual({
      kind: 'connect',
      origin: 'https://mowdeck.lan',
      insecure: false,
    });
    expect(get).toHaveBeenNthCalledWith(2, 'https://mowdeck.lan/manifest.webmanifest');
  });

  it('never loops: a second redirect is refused', async () => {
    const get = vi
      .fn()
      .mockResolvedValue({ status: 301, data: '', location: 'https://mowdeck.lan/' });
    expect(await checkServer('http://mowdeck.lan', get)).toEqual({
      kind: 'not-mowdeck',
      origin: 'https://mowdeck.lan',
    });
    expect(get).toHaveBeenCalledTimes(2);
  });

  it('never probes an address with another scheme', async () => {
    const get = vi.fn();
    const inputs = [
      'file:///etc/passwd',
      'javascript:alert(1)',
      'data:text/html,x',
      'capacitor://localhost',
      'ftp://x.lan',
    ];
    for (const input of inputs) {
      expect(await checkServer(input, get)).toMatchObject({ kind: 'invalid' });
    }
    expect(get).not.toHaveBeenCalled();
  });
});

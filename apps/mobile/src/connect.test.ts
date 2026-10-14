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
      kind: 'ok',
      origin: 'https://mowdeck.lan',
      insecure: false,
    });
    expect(get).toHaveBeenCalledWith('https://mowdeck.lan/manifest.webmanifest');
  });

  it('reports another site, an error status or a failed request as unreachable', async () => {
    const other = vi.fn().mockResolvedValue({ status: 200, data: { name: 'Router' } });
    const missing = vi.fn().mockResolvedValue({ status: 404, data: '' });
    const failed = vi.fn().mockRejectedValue(new Error('TLS'));
    for (const get of [other, missing, failed]) {
      expect(await checkServer('http://mowdeck.lan', get)).toEqual({
        kind: 'unreachable',
        origin: 'http://mowdeck.lan',
        insecure: true,
      });
    }
  });
});

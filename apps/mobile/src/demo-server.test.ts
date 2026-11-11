// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { describe, expect, it, vi } from 'vitest';

import { checkServer } from './connect';
import { DEMO_SERVER_URL } from './demo-server';
import { normalizeServerUrl } from './server-url';

describe('the demo server address', () => {
  it('passes the same validation as a typed address, secure and without a path', () => {
    expect(normalizeServerUrl(DEMO_SERVER_URL)).toEqual({
      ok: true,
      origin: DEMO_SERVER_URL,
      insecure: false,
    });
  });

  it('is connected through the normal server check', async () => {
    const get = vi.fn().mockResolvedValue({ status: 200, data: { name: 'MowDeck' } });
    await expect(checkServer(DEMO_SERVER_URL, get)).resolves.toEqual({
      kind: 'connect',
      origin: DEMO_SERVER_URL,
      insecure: false,
    });
    expect(get).toHaveBeenCalledWith(`${DEMO_SERVER_URL}/manifest.webmanifest`);
  });

  it('reports an unreachable demo like any unreachable server', async () => {
    const get = vi.fn().mockRejectedValue(new Error('offline'));
    await expect(checkServer(DEMO_SERVER_URL, get)).resolves.toEqual({
      kind: 'unreachable',
      origin: DEMO_SERVER_URL,
      insecure: false,
    });
  });
});

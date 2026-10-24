import { describe, expect, it, vi } from 'vitest';

import { SessionManager } from '#lib/auth/session.svelte.js';

import { nativeBridge, type CapacitorBridge } from './capacitor';
import {
  appLoginRequest,
  callbackUrl,
  HandoffError,
  handoffAvailable,
  pkcePair,
  signInThroughBrowser,
} from './handoff';
import { authCallback, pathFromDeepLink } from './links';

const CHALLENGE = 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM';

/** A bridge with `MowDeckShell.openAuthSession` (or without it, for older app builds). */
function fakeBridge(
  platform: 'ios' | 'android',
  open: (options: object) => unknown,
  method = true,
) {
  const listeners: ((data: unknown) => void)[] = [];
  const cap: CapacitorBridge = {
    isNativePlatform: () => true,
    getPlatform: () => platform,
    PluginHeaders: [
      { name: 'App', methods: [{ name: 'getLaunchUrl' }] },
      { name: 'MowDeckShell', methods: method ? [{ name: 'openAuthSession' }] : [] },
    ],
    nativePromise: async (plugin, name, options) => {
      if (plugin === 'MowDeckShell' && name === 'openAuthSession') return open(options ?? {});
      return undefined;
    },
    addListener: (_plugin, _event, callback) => {
      listeners.push(callback);
      return { remove: () => listeners.splice(listeners.indexOf(callback), 1) };
    },
  };
  const bridge = nativeBridge({ Capacitor: cap });
  if (!bridge) throw new Error('no bridge');
  return { bridge, emit: (url: string) => [...listeners].forEach((l) => l({ url })), listeners };
}

function opened(options: object): URL {
  return new URL((options as { url: string }).url);
}

describe('app login hand-off: links and parameters', () => {
  it('reads the app parameters of the sign-in page and builds the callback', () => {
    const state = 'abcdefghijklmnopqrstuv';
    expect(
      appLoginRequest(new URLSearchParams({ app_challenge: CHALLENGE, app_state: state })),
    ).toEqual({ challenge: CHALLENGE, state });
    expect(appLoginRequest(new URLSearchParams({ app_challenge: CHALLENGE }))).toBeNull();
    expect(
      appLoginRequest(new URLSearchParams({ app_challenge: 'short', app_state: state })),
    ).toBeNull();
    expect(
      appLoginRequest(new URLSearchParams({ app_challenge: CHALLENGE, app_state: 'x<script>' })),
    ).toBeNull();
    expect(callbackUrl('mda_a+b', state)).toBe(
      `mowdeck://auth/callback?code=mda_a%2Bb&state=${state}`,
    );
  });

  it('parses auth callbacks and never routes them as pages', () => {
    expect(authCallback('mowdeck://auth/callback?code=c&state=s')).toEqual({
      code: 'c',
      state: 's',
      error: null,
    });
    expect(authCallback('mowdeck://mowers/1')).toBeNull();
    expect(authCallback('https://auth/callback?code=c')).toBeNull();
    expect(pathFromDeepLink('mowdeck://auth/callback?code=c&state=s')).toBeNull();
    expect(pathFromDeepLink('mowdeck://mowers/1')).toBe('/mowers/1');
  });

  it('makes an RFC 7636 S256 pair', async () => {
    // Appendix B of RFC 7636: these 32 bytes give the verifier dBjftJeZ4CVP-...
    const octets = [
      116, 24, 223, 180, 151, 153, 224, 37, 79, 250, 96, 125, 216, 173, 187, 186, 22, 212, 37, 77,
      105, 214, 191, 240, 91, 88, 5, 88, 83, 132, 141, 121,
    ];
    const { verifier, challenge } = await pkcePair(() => new Uint8Array(octets));
    expect(verifier).toBe('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk');
    expect(challenge).toBe(CHALLENGE);
  });

  it('needs a shell build with openAuthSession', () => {
    expect(handoffAvailable(fakeBridge('ios', () => ({})).bridge)).toBe(true);
    expect(handoffAvailable(fakeBridge('ios', () => ({}), false).bridge)).toBe(false);
    expect(handoffAvailable(null)).toBe(false);
  });
});

describe('app login hand-off: the flow', () => {
  it('iOS: the session returns the callback URL; the verifier redeems the code', async () => {
    const redeem = vi.fn(async () => undefined);
    let page: URL | null = null;
    const { bridge, listeners } = fakeBridge('ios', (options) => {
      page = opened(options);
      const state = page.searchParams.get('app_state') ?? '';
      expect((options as { callbackScheme: string }).callbackScheme).toBe('mowdeck');
      return { url: callbackUrl('mda_code', state) };
    });
    const result = await signInThroughBrowser({
      bridge,
      origin: 'https://mowdeck.example.org',
      redeem,
    });
    expect(result).toBe('signed-in');
    expect(page!.origin + page!.pathname).toBe('https://mowdeck.example.org/login');
    const [code, verifier] = redeem.mock.calls[0] as unknown as [string, string];
    expect(code).toBe('mda_code');
    // The challenge in the URL is the S256 hash of the verifier the app kept.
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
    const expected = btoa(String.fromCharCode(...new Uint8Array(digest)))
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
      .replace(/=+$/, '');
    expect(page!.searchParams.get('app_challenge')).toBe(expected);
    expect(page!.search).not.toContain(verifier);
    expect(listeners).toHaveLength(0);
  });

  it('Android: the Custom Tab returns through a deep link', async () => {
    const redeem = vi.fn(async () => undefined);
    let state = '';
    const fake = fakeBridge('android', (options) => {
      state = opened(options).searchParams.get('app_state') ?? '';
      return {};
    });
    const pending = signInThroughBrowser({
      bridge: fake.bridge,
      origin: 'https://m.example',
      redeem,
    });
    await vi.waitFor(() => expect(state).not.toBe(''));
    fake.emit('mowdeck://mowers/1'); // other deep links are not the answer
    fake.emit(callbackUrl('mda_x', state));
    expect(await pending).toBe('signed-in');
    expect(redeem).toHaveBeenCalledWith('mda_x', expect.any(String));
  });

  it('refuses a callback with a foreign state', async () => {
    const redeem = vi.fn();
    const { bridge } = fakeBridge('ios', () => ({
      url: callbackUrl('mda_evil', 'someone-elses-state'),
    }));
    await expect(
      signInThroughBrowser({ bridge, origin: 'https://m.example', redeem }),
    ).rejects.toEqual(new HandoffError('state'));
    expect(redeem).not.toHaveBeenCalled();
  });

  it('treats a closed browser, an abort and a cancel callback as cancelled', async () => {
    const redeem = vi.fn();
    const closed = fakeBridge('ios', () => {
      throw Object.assign(new Error('The user cancelled'), { code: 'cancelled' });
    });
    expect(await signInThroughBrowser({ bridge: closed.bridge, origin: 'https://m', redeem })).toBe(
      'cancelled',
    );
    const android = fakeBridge('android', () => ({}));
    const abort = new AbortController();
    const pending = signInThroughBrowser(
      { bridge: android.bridge, origin: 'https://m', redeem },
      abort.signal,
    );
    abort.abort();
    expect(await pending).toBe('cancelled');
    let state = '';
    const said = fakeBridge('ios', (options) => {
      state = opened(options).searchParams.get('app_state') ?? '';
      return { url: `mowdeck://auth/callback?error=cancelled&state=${state}` };
    });
    expect(await signInThroughBrowser({ bridge: said.bridge, origin: 'https://m', redeem })).toBe(
      'cancelled',
    );
    expect(redeem).not.toHaveBeenCalled();
  });

  it('gives up after the timeout', async () => {
    const { bridge } = fakeBridge('android', () => ({}));
    await expect(
      signInThroughBrowser({ bridge, origin: 'https://m', redeem: vi.fn(), timeoutMs: 20 }),
    ).rejects.toEqual(new HandoffError('timeout'));
  });
});

describe('app login hand-off: the browser side of the session', () => {
  it('exchanges the fresh login for a code and ends the browser session', async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      if (String(input).endsWith('/session')) {
        return new Response(JSON.stringify({ access_token: 'acc', expires_in: 600 }));
      }
      if (String(input).endsWith('/me')) {
        return new Response(JSON.stringify({ subject: 'u', roles: [], permissions: [] }));
      }
      return new Response(JSON.stringify({ code: 'mda_1', expires_in: 60 }));
    });
    const session = new SessionManager({
      fetch: fetch as unknown as typeof globalThis.fetch,
      now: () => 0,
      locks: null,
      channel: null,
    });
    await session.login('lukas', 'pw');
    const code = await session.handOffToApp(CHALLENGE);
    expect(code).toBe('mda_1');
    const handoff = calls.at(-1)!;
    expect(handoff.url).toBe('/api/v1/identity/app-login/code');
    expect((handoff.init?.headers as Record<string, string>).Authorization).toBe('Bearer acc');
    expect(JSON.parse(String(handoff.init?.body))).toEqual({ code_challenge: CHALLENGE });
    expect(session.status).toBe('anonymous');
    expect(session.token).toBeNull();
  });
});

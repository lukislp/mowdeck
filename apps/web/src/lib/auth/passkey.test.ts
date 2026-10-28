import { describe, expect, it, vi } from 'vitest';

import {
  deviceHint,
  deviceName,
  passkeysEnabled,
  providerName,
  registerPasskey,
  signInWithPasskey,
} from './passkey';
import { SessionManager } from './session.svelte';
import {
  cancelled,
  conditionalMediationAvailable,
  credentialJson,
  creationOptions,
  fromBase64url,
  requestOptions,
  toBase64url,
  webauthnSupported,
} from './webauthn';

function json(status: number, body?: unknown): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const bytes = (...values: number[]) => new Uint8Array(values).buffer;

/** What a browser without `toJSON()` hands back from `navigator.credentials.get()`. */
function assertion(): Credential {
  return {
    id: 'AQID',
    rawId: bytes(1, 2, 3),
    type: 'public-key',
    authenticatorAttachment: 'platform',
    response: {
      clientDataJSON: bytes(123, 125),
      authenticatorData: bytes(9, 9),
      signature: bytes(7),
      userHandle: bytes(0xff, 0xfe),
    },
    getClientExtensionResults: () => ({}),
  } as unknown as Credential;
}

describe('webauthn codec', () => {
  it('round-trips base64url without padding', () => {
    const data = new Uint8Array([0, 0xff, 0xfe, 1, 2]);
    const text = toBase64url(data);
    expect(text).toBe('AP_-AQI');
    expect([...fromBase64url(text)]).toEqual([...data]);
  });

  it('decodes request and creation options when the browser has no JSON parser', () => {
    const request = requestOptions(
      { challenge: 'AQID', rpId: 'mowdeck.example.org', userVerification: 'required' },
      {},
    );
    expect([...new Uint8Array(request.challenge as ArrayBuffer)]).toEqual([1, 2, 3]);
    expect(request.allowCredentials).toEqual([]);
    expect(request.userVerification).toBe('required');
    const creation = creationOptions(
      {
        challenge: 'AQID',
        rp: { id: 'mowdeck.example.org', name: 'MowDeck' },
        user: { id: '_w', name: 'lukas', displayName: 'Lukas' },
        pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
        excludeCredentials: [{ id: 'AQ', type: 'public-key', transports: ['internal'] }],
      },
      {},
    );
    expect([...new Uint8Array(creation.user.id as ArrayBuffer)]).toEqual([0xff]);
    expect(creation.excludeCredentials?.[0]?.transports).toEqual(['internal']);
    expect(creation.rp.id).toBe('mowdeck.example.org');
  });

  it('uses the native JSON parsers where the browser has them', () => {
    const parseRequestOptionsFromJSON = vi.fn(() => ({ challenge: new ArrayBuffer(1) }));
    const scope = { PublicKeyCredential: { parseRequestOptionsFromJSON } };
    requestOptions({ challenge: 'AA' }, scope);
    expect(parseRequestOptionsFromJSON).toHaveBeenCalledWith({ challenge: 'AA' });
  });

  it('serialises a credential like toJSON()', () => {
    expect(credentialJson(assertion())).toEqual({
      id: 'AQID',
      rawId: 'AQID',
      type: 'public-key',
      authenticatorAttachment: 'platform',
      response: {
        clientDataJSON: 'e30',
        authenticatorData: 'CQk',
        signature: 'Bw',
        userHandle: '__4',
      },
      clientExtensionResults: {},
    });
    const native = { toJSON: () => ({ id: 'x' }) } as unknown as Credential;
    expect(credentialJson(native)).toEqual({ id: 'x' });
  });

  it('detects support, conditional mediation and cancellation', async () => {
    const credentials = {};
    expect(webauthnSupported({})).toBe(false);
    expect(
      webauthnSupported({
        PublicKeyCredential: {},
        isSecureContext: true,
        navigator: { credentials },
      }),
    ).toBe(true);
    expect(
      webauthnSupported({
        PublicKeyCredential: {},
        isSecureContext: false,
        navigator: { credentials },
      }),
    ).toBe(false);
    expect(await conditionalMediationAvailable({})).toBe(false);
    expect(
      await conditionalMediationAvailable({
        PublicKeyCredential: { isConditionalMediationAvailable: () => Promise.resolve(true) },
      }),
    ).toBe(true);
    expect(
      await conditionalMediationAvailable({
        PublicKeyCredential: { isConditionalMediationAvailable: () => Promise.reject(new Error()) },
      }),
    ).toBe(false);
    expect(cancelled(new DOMException('x', 'NotAllowedError'))).toBe(true);
    expect(cancelled(new DOMException('x', 'AbortError'))).toBe(true);
    expect(cancelled(new Error('x'))).toBe(false);
  });
});

describe('passkey login', () => {
  function env(finish = json(200, { access_token: 'acc', expires_in: 600 })) {
    const calls: { url: string; body: unknown }[] = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
      if (url.endsWith('/passkeys/challenge')) {
        return json(200, {
          ceremony_id: 'c1',
          public_key: { challenge: 'AQID', rpId: 'mowdeck.example.org' },
        });
      }
      if (url.endsWith('/passkeys/session')) return finish;
      if (url.endsWith('/me')) return json(200, { subject: 'u1', permissions: [], roles: [] });
      return json(404);
    });
    const session = new SessionManager({
      fetch: fetch as unknown as typeof globalThis.fetch,
      now: () => 0,
      locks: null,
      channel: null,
    });
    const get = vi.fn(async () => assertion());
    return { session, fetch, calls, credentials: { get, create: vi.fn() } };
  }

  it('answers the challenge without a username and starts the session', async () => {
    const e = env();
    await signInWithPasskey(e.session, { mediation: 'conditional' }, e);
    const options = (e.credentials.get.mock.calls[0] as unknown[])[0] as CredentialRequestOptions;
    expect(options.mediation).toBe('conditional');
    expect(options.publicKey?.rpId).toBe('mowdeck.example.org');
    expect(e.calls.map((c) => c.url)).toEqual([
      '/api/v1/identity/passkeys/challenge',
      '/api/v1/identity/passkeys/session',
      '/api/v1/identity/me',
    ]);
    expect(e.calls[1]?.body).toMatchObject({ ceremony_id: 'c1', credential: { id: 'AQID' } });
    expect(e.session.status).toBe('authenticated');
  });

  it('tells when the authenticator has produced a credential, and not when it failed', async () => {
    const e = env();
    const onChosen = vi.fn();
    await signInWithPasskey(e.session, { onChosen }, e);
    expect(onChosen).toHaveBeenCalledTimes(1);
    const unsupported = env();
    unsupported.credentials.get.mockRejectedValueOnce(new DOMException('no', 'NotSupportedError'));
    const never = vi.fn();
    await expect(
      signInWithPasskey(unsupported.session, { onChosen: never }, unsupported),
    ).rejects.toMatchObject({ name: 'NotSupportedError' });
    expect(never).not.toHaveBeenCalled();
    const refused = env(json(401, { status: 401, title: 'Unauthorized' }));
    const after = vi.fn();
    await expect(
      signInWithPasskey(refused.session, { onChosen: after }, refused),
    ).rejects.toMatchObject({ status: 401 });
    expect(after).toHaveBeenCalledTimes(1);
  });

  it('reports a refused passkey as a problem and a cancelled dialog as a DOMException', async () => {
    const refused = env(json(401, { status: 401, title: 'Unauthorized' }));
    await expect(signInWithPasskey(refused.session, {}, refused)).rejects.toMatchObject({
      status: 401,
    });
    const dismissed = env();
    dismissed.credentials.get.mockRejectedValueOnce(new DOMException('no', 'NotAllowedError'));
    await expect(signInWithPasskey(dismissed.session, {}, dismissed)).rejects.toSatisfy(cancelled);
    expect(dismissed.session.status).not.toBe('authenticated');
  });

  it('asks Identity whether passkeys are on, and never throws', async () => {
    const on = vi.fn(async () => json(200, { password: true, passkey: true }));
    const off = vi.fn(async () => json(200, { password: true, passkey: false }));
    const down = vi.fn(async () => {
      throw new TypeError('offline');
    });
    expect(await passkeysEnabled(on as unknown as typeof fetch)).toBe(true);
    expect(await passkeysEnabled(off as unknown as typeof fetch)).toBe(false);
    expect(await passkeysEnabled(down as unknown as typeof fetch)).toBe(false);
  });
});

describe('passkey registration', () => {
  it('re-authenticates, creates the credential and sends it with the name', async () => {
    const finish = vi.fn(async () => ({}));
    const api = {
      options: vi.fn(async () => ({
        ceremony_id: 'c2',
        public_key: {
          challenge: 'AQID',
          rp: { id: 'mowdeck.example.org', name: 'MowDeck' },
          user: { id: 'AA', name: 'lukas', displayName: 'Lukas' },
          pubKeyCredParams: [],
        },
      })),
      finish,
    };
    const created = {
      ...assertion(),
      id: 'AQID',
      rawId: bytes(1, 2, 3),
      type: 'public-key',
      response: {
        clientDataJSON: bytes(123, 125),
        attestationObject: bytes(1),
        getTransports: () => ['internal'],
      },
    } as unknown as Credential;
    const create = vi.fn(async () => created);
    await registerPasskey(api, 'secret', 'Mac', { create });
    expect(api.options).toHaveBeenCalledWith('secret');
    expect(finish).toHaveBeenCalledWith({
      ceremony_id: 'c2',
      name: 'Mac',
      credential: expect.objectContaining({
        response: { clientDataJSON: 'e30', attestationObject: 'AQ', transports: ['internal'] },
      }),
    });
  });

  it('stops when the device creates nothing', async () => {
    const api = {
      options: vi.fn(async () => ({
        ceremony_id: 'c',
        public_key: { challenge: 'AA', rp: { name: 'M' }, user: { id: 'AA', name: 'x' } },
      })),
      finish: vi.fn(),
    };
    await expect(registerPasskey(api, 'pw', null, { create: async () => null })).rejects.toSatisfy(
      cancelled,
    );
    expect(api.finish).not.toHaveBeenCalled();
  });
});

describe('device hints', () => {
  const iphone =
    'Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1';
  const windows =
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36 Edg/141.0';
  it('names devices and providers', () => {
    expect(deviceName(iphone)).toBe('iPhone');
    expect(deviceName('Mozilla/5.0 (Linux; Android 16)')).toBe('Android');
    expect(deviceName('curl')).toBe('Passkey');
    expect(deviceHint(iphone)).toBe('Safari · iPhone');
    expect(deviceHint(windows)).toBe('Edge · Windows');
    expect(deviceHint(null)).toBeNull();
    expect(providerName('FBFC3007-154E-4ECC-8C0B-6E020557D7BD')).toBe('iCloud Keychain');
    expect(providerName('00000000-0000-0000-0000-000000000001')).toBeNull();
    expect(providerName(null)).toBeNull();
  });
});

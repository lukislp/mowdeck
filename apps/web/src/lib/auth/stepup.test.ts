import { describe, expect, it, vi } from 'vitest';

import { authFetch } from '#lib/api/client.js';

import { REAUTH_TYPE, StepUp, isReauthRequired, type StepUpDeps } from './stepup.svelte';

function reauthResponse(): Response {
  return new Response(
    JSON.stringify({ type: REAUTH_TYPE, status: 401, title: 'Re-authentication required' }),
    {
      status: 401,
    },
  );
}

function setup(over: Partial<StepUpDeps> = {}, fetchFn?: typeof fetch) {
  const session = { token: 'old' as string | null, adopt: vi.fn() };
  const deps: StepUpDeps = {
    fetch:
      fetchFn ??
      (vi.fn(async () =>
        Response.json({ access_token: 'fresh', expires_in: 600 }),
      ) as unknown as typeof fetch),
    credentials: null,
    passkeyAllowed: () => false,
    passkeysEnabled: async () => true,
    ...over,
  };
  return { session, deps, stepUp: new StepUp(session, deps) };
}

describe('isReauthRequired', () => {
  it('only matches the 401 reauth-required problem', async () => {
    expect(await isReauthRequired(reauthResponse())).toBe(true);
    expect(await isReauthRequired(new Response('{}', { status: 401 }))).toBe(false);
    expect(
      await isReauthRequired(new Response(JSON.stringify({ type: REAUTH_TYPE }), { status: 403 })),
    ).toBe(false);
    expect(await isReauthRequired(new Response('nope', { status: 401 }))).toBe(false);
  });
});

describe('StepUp', () => {
  it('opens one sheet for concurrent requests and resolves them with the new token', async () => {
    const { stepUp, session } = setup();
    const a = stepUp.request();
    const b = stepUp.request();
    expect(stepUp.open).toBe(true);
    await stepUp.withPassword('secret secret');
    expect(await a).toBe('fresh');
    expect(await b).toBe('fresh');
    expect(stepUp.open).toBe(false);
    expect(session.adopt).toHaveBeenCalledWith('fresh', 600);
  });

  it('sends the password with the session token and keeps the sheet open on a wrong password', async () => {
    const fetchFn = vi.fn(async (_url: unknown, init?: RequestInit) => {
      expect((init?.headers as Record<string, string>).Authorization).toBe('Bearer old');
      return new Response(
        JSON.stringify({ type: 'urn:mowdeck:problem:unauthorized', status: 401, title: 'x' }),
        { status: 401 },
      );
    });
    const { stepUp, session } = setup({}, fetchFn as unknown as typeof fetch);
    const pending = stepUp.request();
    await stepUp.withPassword('wrong');
    expect(stepUp.open).toBe(true);
    expect(stepUp.problem?.status).toBe(401);
    expect(session.adopt).not.toHaveBeenCalled();
    stepUp.cancel();
    expect(await pending).toBeNull();
  });

  it('offers the passkey only in a browser with passkeys on', async () => {
    const off = setup({ passkeyAllowed: () => false, credentials: { get: vi.fn() } });
    void off.stepUp.request();
    await Promise.resolve();
    await Promise.resolve();
    expect(off.stepUp.passkey).toBe(false);

    const on = setup({ passkeyAllowed: () => true, credentials: { get: vi.fn() } });
    void on.stepUp.request();
    await Promise.resolve();
    await Promise.resolve();
    expect(on.stepUp.passkey).toBe(true);
  });

  it('treats a dismissed passkey dialog as no verdict', async () => {
    const fetchFn = vi.fn(async () =>
      Response.json({ ceremony_id: 'c', public_key: { challenge: 'AAAA', rpId: 'x' } }),
    );
    const get = vi.fn(async () => {
      throw new DOMException('closed', 'NotAllowedError');
    });
    const { stepUp } = setup({ credentials: { get } }, fetchFn as unknown as typeof fetch);
    void stepUp.request();
    await stepUp.withPasskey();
    expect(stepUp.cancelledPasskey).toBe(true);
    expect(stepUp.open).toBe(true);
    expect(stepUp.problem).toBeNull();
  });
});

describe('authFetch with step-up', () => {
  const tokens = () => ({ validToken: vi.fn(async () => 'old'), expire: vi.fn() });

  it('repeats the request with the confirmed token and never ends the session', async () => {
    const seen: string[] = [];
    const base = vi.fn(async (request: Request) => {
      seen.push(request.headers.get('Authorization') ?? '');
      return seen.length === 1 ? reauthResponse() : new Response(null, { status: 204 });
    });
    const t = tokens();
    const reauth = vi.fn(async () => 'fresh');
    const response = await authFetch(
      t,
      base as never,
      1000,
      reauth,
    )('http://x/api/v1/fleet/mowers/1', {
      method: 'DELETE',
    });
    expect(response.status).toBe(204);
    expect(seen).toEqual(['Bearer old', 'Bearer fresh']);
    expect(reauth).toHaveBeenCalledOnce();
    expect(t.expire).not.toHaveBeenCalled();
    expect(t.validToken).toHaveBeenCalledOnce();
  });

  it('returns the problem when the sheet is dismissed', async () => {
    const base = vi.fn(async () => reauthResponse());
    const t = tokens();
    const response = await authFetch(
      t,
      base as never,
      1000,
      async () => null,
    )('http://x/y', {
      method: 'DELETE',
    });
    expect(response.status).toBe(401);
    expect(base).toHaveBeenCalledOnce();
    expect(t.expire).not.toHaveBeenCalled();
  });
});

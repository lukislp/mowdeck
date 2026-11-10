// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import createClient from 'openapi-fetch';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiProblem } from '#lib/api/problem.js';
import type { paths } from '#lib/generated/vision.js';
import { m } from '#lib/paraglide/messages.js';
import { setLocale } from '#lib/paraglide/runtime.js';
import type { VisionApi } from '#lib/vision/api.js';
import { AssistStore } from '#lib/vision/assist.svelte.js';

import {
  ConsentState,
  DECLINED,
  consentCalls,
  consentGiven,
  isConsentProblem,
  type ConsentApi,
  type ConsentStatus,
} from './consent.svelte.js';
import ConsentSheet from './ConsentSheet.svelte';

const CLOUD = {
  kind: 'cloud',
  provider: 'Anthropic',
  family: 'anthropic',
  model: 'anthropic/claude-sonnet-5-5',
  endpoint: 'default',
} as const;

function status(over: Partial<ConsentStatus> = {}): ConsentStatus {
  return {
    required: true,
    destination: CLOUD,
    granted_at: null,
    granted_for: null,
    covers: false,
    ...over,
  };
}

const GRANTED = status({
  granted_at: '2026-11-10T08:00:00Z',
  granted_for: CLOUD,
  covers: true,
});

function api(first: ConsentStatus = status(), granted: ConsentStatus = GRANTED) {
  const calls = {
    status: vi.fn(async () => first),
    grant: vi.fn(async () => granted),
    withdraw: vi.fn(async () => status()),
  };
  return { calls, api: calls satisfies ConsentApi };
}

function problem(slug: string, status = 403): ApiProblem {
  return new ApiProblem({ type: `urn:mowdeck:problem:${slug}`, status });
}

beforeEach(() => setLocale('en', { reload: false }));

describe('consent rules of the client', () => {
  it('needs nothing when the provider needs no consent or the consent covers it', () => {
    expect(consentGiven(status({ required: false }))).toBe(true);
    expect(consentGiven(GRANTED)).toBe(true);
    expect(consentGiven(status())).toBe(false);
  });

  it('knows the consent problems of Vision and of the assistant', () => {
    expect(isConsentProblem(problem('vision-consent-required'))).toBe(true);
    expect(isConsentProblem(problem('assistant-consent-required'))).toBe(true);
    expect(isConsentProblem(problem('feature-not-in-plan'))).toBe(false);
    expect(isConsentProblem(problem('vision-consent-required', 409))).toBe(false);
    expect(isConsentProblem(new Error('offline'))).toBe(false);
  });

  it('calls the consent endpoints', async () => {
    const seen: string[] = [];
    const fetcher = vi.fn(async (request: Request) => {
      seen.push(`${request.method} ${new URL(request.url).pathname}`);
      return Response.json(GRANTED);
    });
    const calls = consentCalls(createClient<paths>({ baseUrl: 'http://x.test', fetch: fetcher }));
    await calls.status();
    await calls.grant();
    await calls.withdraw();
    expect(seen).toEqual([
      'GET /api/v1/vision/me/consent',
      'PUT /api/v1/vision/me/consent',
      'DELETE /api/v1/vision/me/consent',
    ]);
  });
});

describe('ConsentState', () => {
  it('lets a request pass without a dialog when no consent is required', async () => {
    const { api: calls } = api(
      status({ required: false, destination: { ...CLOUD, kind: 'local' } }),
    );
    const state = new ConsentState(calls);
    expect(await state.ensure('assist')).toBe(true);
    expect(state.open).toBe(false);
  });

  it('lets a request pass when the status cannot be read: the service decides then', async () => {
    const state = new ConsentState({
      status: async () => {
        throw ApiProblem.network();
      },
      grant: vi.fn(),
      withdraw: vi.fn(),
    });
    expect(await state.ensure()).toBe(true);
  });

  it('opens the dialog and resolves true after agree', async () => {
    const { api: calls, calls: spies } = api();
    const state = new ConsentState(calls);
    const asked = state.ensure('chat');
    await waitFor(() => expect(state.open).toBe(true));
    expect(state.scope).toBe('chat');
    await state.agree();
    expect(await asked).toBe(true);
    expect(spies.grant).toHaveBeenCalledOnce();
    expect(state.open).toBe(false);
    expect(state.status?.covers).toBe(true);
  });

  it('resolves false after not now, without a grant', async () => {
    const { api: calls, calls: spies } = api();
    const state = new ConsentState(calls);
    const asked = state.ensure();
    await waitFor(() => expect(state.open).toBe(true));
    state.decline();
    expect(await asked).toBe(false);
    expect(spies.grant).not.toHaveBeenCalled();
    expect(state.open).toBe(false);
  });

  it('shares one dialog between concurrent callers', async () => {
    const { api: calls, calls: spies } = api();
    const state = new ConsentState(calls);
    const a = state.ensure();
    const b = state.ensure();
    await waitFor(() => expect(state.open).toBe(true));
    await state.agree();
    expect(await Promise.all([a, b])).toEqual([true, true]);
    expect(spies.grant).toHaveBeenCalledOnce();
  });

  it('keeps the dialog open with the problem when the grant fails', async () => {
    const { api: calls, calls: spies } = api();
    spies.grant.mockRejectedValueOnce(problem('unavailable', 503));
    const state = new ConsentState(calls);
    const asked = state.ensure();
    await waitFor(() => expect(state.open).toBe(true));
    await state.agree();
    expect(state.open).toBe(true);
    expect(state.problem?.status).toBe(503);
    await state.agree();
    expect(await asked).toBe(true);
  });

  it('guard asks first and runs the request after agree', async () => {
    const { api: calls } = api();
    const state = new ConsentState(calls);
    const run = vi.fn(async () => 'sent');
    const result = state.guard('assist', run);
    await waitFor(() => expect(state.open).toBe(true));
    expect(run).not.toHaveBeenCalled();
    await state.agree();
    expect(await result).toBe('sent');
    expect(run).toHaveBeenCalledOnce();
  });

  it('guard returns DECLINED and never sends when the user says not now', async () => {
    const { api: calls } = api();
    const state = new ConsentState(calls);
    const run = vi.fn(async () => 'sent');
    const result = state.guard('assist', run);
    await waitFor(() => expect(state.open).toBe(true));
    state.decline();
    expect(await result).toBe(DECLINED);
    expect(run).not.toHaveBeenCalled();
  });

  it('guard asks again and repeats the request once when the service still refuses', async () => {
    // The status said fine (stale), the service knows better: the provider changed meanwhile.
    const covered = api(GRANTED);
    const state = new ConsentState(covered.api);
    const run = vi
      .fn<() => Promise<string>>()
      .mockRejectedValueOnce(problem('vision-consent-required'))
      .mockResolvedValueOnce('sent');
    covered.calls.status.mockResolvedValueOnce(GRANTED).mockResolvedValueOnce(status());
    const result = state.guard('assist', run);
    await waitFor(() => expect(state.open).toBe(true));
    await state.agree();
    expect(await result).toBe('sent');
    expect(run).toHaveBeenCalledTimes(2);
  });

  it('guard passes other errors on unchanged', async () => {
    const state = new ConsentState(api(status({ required: false })).api);
    await expect(
      state.guard('assist', async () => {
        throw problem('vision-quota', 429);
      }),
    ).rejects.toMatchObject({ status: 429 });
  });

  it('withdraws through the service', async () => {
    const { api: calls, calls: spies } = api(GRANTED);
    const state = new ConsentState(calls);
    expect((await state.withdraw()).covers).toBe(false);
    expect(spies.withdraw).toHaveBeenCalledOnce();
    expect(state.status?.granted_at).toBeNull();
  });
});

describe('ConsentSheet', () => {
  async function opened(scope: 'assist' | 'chat' | 'any') {
    const { api: calls, calls: spies } = api();
    const state = new ConsentState(calls);
    const asked = state.ensure(scope);
    await waitFor(() => expect(state.open).toBe(true));
    render(ConsentSheet, { props: { consent: state } });
    return { state, asked, spies };
  }

  it('names what is sent, to whom, that it leaves, that it is optional and how to withdraw', async () => {
    const { state, asked } = await opened('any');
    const body = await screen.findByTestId('consent-body');
    expect(screen.getByRole('dialog', { name: m.consent_title() })).toBeInTheDocument();
    expect(body).toHaveTextContent(
      m.consent_intro({ provider: 'Anthropic', model: 'anthropic/claude-sonnet-5-5' }),
    );
    for (const text of [
      m.consent_sends_pictures(),
      m.consent_sends_map(),
      m.consent_sends_status(),
      m.consent_sends_text(),
      m.consent_sends_names(),
      m.consent_leaves({ provider: 'Anthropic' }),
      m.consent_optional(),
      m.consent_withdraw(),
    ]) {
      expect(body).toHaveTextContent(text);
    }
    state.decline();
    await asked;
  });

  it('names the host only when the data goes to a configured address', async () => {
    const plain = await opened('any');
    expect(screen.queryByTestId('consent-endpoint')).toBeNull();
    plain.state.decline();
    await plain.asked;
    const { api: calls } = api(
      status({ destination: { ...CLOUD, endpoint: 'llm.other.example' } }),
    );
    const state = new ConsentState(calls);
    const asked = state.ensure('any');
    await waitFor(() => expect(state.open).toBe(true));
    render(ConsentSheet, { props: { consent: state } });
    expect(await screen.findByTestId('consent-endpoint')).toHaveTextContent('llm.other.example');
    state.decline();
    await asked;
  });

  it('lists only the pictures for assist and only the text for the chat', async () => {
    const assist = await opened('assist');
    expect(screen.getByText(m.consent_sends_pictures())).toBeInTheDocument();
    expect(screen.queryByText(m.consent_sends_text())).toBeNull();
    assist.state.decline();
    await assist.asked;
  });

  it('shows the chat list for the chat', async () => {
    const chat = await opened('chat');
    expect(screen.getByText(m.consent_sends_text())).toBeInTheDocument();
    expect(screen.queryByText(m.consent_sends_pictures())).toBeNull();
    chat.state.decline();
    await chat.asked;
  });

  it('agrees with the button', async () => {
    const { asked, spies } = await opened('any');
    await fireEvent.click(await screen.findByTestId('consent-agree'));
    expect(await asked).toBe(true);
    expect(spies.grant).toHaveBeenCalledOnce();
  });

  it('declines with the button, Escape and the close button, and shows no error', async () => {
    const button = await opened('any');
    await fireEvent.click(await screen.findByTestId('consent-decline'));
    expect(await button.asked).toBe(false);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByTestId('consent-sheet')).toBeNull();
  });

  it('declines with Escape', async () => {
    const escape = await opened('any');
    await fireEvent.keyDown(window, { key: 'Escape' });
    expect(await escape.asked).toBe(false);
  });

  it('moves focus into the dialog and back to the opener', async () => {
    const opener = document.createElement('button');
    document.body.append(opener);
    opener.focus();
    const { api: calls } = api();
    const state = new ConsentState(calls);
    render(ConsentSheet, { props: { consent: state } });
    const asked = state.ensure('any');
    await waitFor(() =>
      expect(screen.getByTestId('consent-body').querySelector('.intro')).toHaveFocus(),
    );
    state.decline();
    await asked;
    await waitFor(() => expect(opener).toHaveFocus());
    opener.remove();
  });
});

describe('AssistStore asks for consent before a request that sends pictures', () => {
  const proposal = { id: 'p1', mower_id: 'm1', final: true, state: 'proposed' } as never;

  function visionApi(over: Partial<VisionApi> = {}): VisionApi {
    return {
      provider: vi.fn(async () => ({ effective: 'hosted' })),
      preferences: vi.fn(async () => ({ stereo_frames: true })),
      proposals: vi.fn(async () => []),
      recover: vi.fn(async () => proposal),
      retry: vi.fn(async () => proposal),
      ...over,
    } as unknown as VisionApi;
  }

  it('does not start without consent and stays quiet', async () => {
    const calls = visionApi();
    const guard = {
      guard: vi.fn(async () => DECLINED),
      ensure: vi.fn(async () => false),
    } as never;
    const store = new AssistStore(calls, undefined, guard);
    await store.open('m1');
    await store.start('recover');
    expect(store.proposal).toBeNull();
    expect(store.problem).toBeNull();
    expect(store.busy).toBeNull();
  });

  it('starts through the guard when consent is given', async () => {
    const calls = visionApi();
    const guard = {
      guard: vi.fn(async (_scope: string, run: () => Promise<unknown>) => run()),
      ensure: vi.fn(async () => true),
    } as never;
    const store = new AssistStore(calls, undefined, guard);
    await store.open('m1');
    await store.start('recover');
    expect(calls.recover).toHaveBeenCalledOnce();
    expect(store.proposal?.id).toBe('p1');
  });

  it('retries through the guard too', async () => {
    const calls = visionApi();
    const run = vi.fn(async (_scope: string, call: () => Promise<unknown>) => call());
    const store = new AssistStore(calls, undefined, { guard: run, ensure: vi.fn() } as never);
    await store.open('m1');
    await store.start('recover');
    await store.retry();
    expect(calls.retry).toHaveBeenCalledOnce();
    expect(run).toHaveBeenCalledTimes(2);
  });

  it('a declined retry leaves the proposal as it is', async () => {
    const calls = visionApi();
    let first = true;
    const guard = {
      guard: vi.fn(async (_scope: string, run: () => Promise<unknown>) => {
        if (first) {
          first = false;
          return run();
        }
        return DECLINED;
      }),
      ensure: vi.fn(),
    } as never;
    const store = new AssistStore(calls, undefined, guard);
    await store.open('m1');
    await store.start('recover');
    await store.retry();
    expect(calls.retry).not.toHaveBeenCalled();
    expect(store.problem).toBeNull();
    expect(store.proposal?.id).toBe('p1');
  });
});

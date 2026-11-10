// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * The features only call the consent module: the chat box asks before the typed text goes to the
 * model, and the AI provider settings show the consent state, withdraw it and ask when
 * automatic analysis is switched on. The consent rules themselves are the service's (see
 * `consent.test.ts` for the client module).
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const fake = vi.hoisted(() => ({
  status: {
    required: true,
    destination: {
      kind: 'cloud',
      provider: 'Anthropic',
      family: 'anthropic',
      model: 'anthropic/claude-sonnet-5-5',
    },
    granted_at: null as string | null,
    granted_for: null as unknown,
    covers: false,
  },
  grant: vi.fn(),
  withdraw: vi.fn(),
  propose: vi.fn(),
  savePreferences: vi.fn(),
}));

vi.mock('#lib/consent/consent.svelte.js', async (original) => {
  const module = await original<typeof import('#lib/consent/consent.svelte.js')>();
  return {
    ...module,
    consent: new module.ConsentState({
      status: async () => structuredClone(fake.status) as never,
      grant: async () => fake.grant(),
      withdraw: async () => fake.withdraw(),
    }),
  };
});
vi.mock('#lib/assistant/assistant.js', async (original) => ({
  ...(await original<typeof import('#lib/assistant/assistant.js')>()),
  loadStatus: async () => ({ ready: true, interpreter: 'provider', model: 'm', hosted: true }),
  propose: (...args: unknown[]) => fake.propose(...args),
}));
vi.mock('#lib/live.svelte.js', () => ({ sendCommand: vi.fn() }));
vi.mock('#lib/vision/api.js', async (original) => ({
  ...(await original<typeof import('#lib/vision/api.js')>()),
  visionCalls: () => ({
    providers: async () => ({
      families: [],
      hosted_available: true,
      hosted_model: 'anthropic/claude-sonnet-5-5',
      secrets_available: true,
    }),
    provider: async () => ({
      configured: false,
      enabled: false,
      model: null,
      family: null,
      api_base: null,
      api_key_set: false,
      header_names: [],
      last_test: null,
      updated_at: null,
      hosted_available: true,
      hosted_model: 'anthropic/claude-sonnet-5-5',
      effective: 'hosted',
      secrets_available: true,
    }),
    preferences: async () => ({
      stereo_frames: true,
      updated_at: null,
      auto_analysis: false,
      auto_analysis_available: true,
      auto_cooldown_minutes: 10,
      auto_daily_per_mower: 20,
    }),
    savePreferences: (body: unknown) => fake.savePreferences(body),
  }),
}));

import { m } from '#lib/paraglide/messages.js';
import { setLocale } from '#lib/paraglide/runtime.js';
import ChatControl from '#lib/components/ChatControl.svelte';
import { consent } from '#lib/consent/consent.svelte.js';
import ConsentSheet from '#lib/consent/ConsentSheet.svelte';
import AiProviderSection from '#lib/settings/sections/AiProviderSection.svelte';

const CLOUD = fake.status.destination;
const GRANTED = {
  required: true,
  destination: CLOUD,
  granted_at: '2026-11-10T08:00:00Z',
  granted_for: CLOUD,
  covers: true,
};

beforeEach(() => {
  setLocale('en', { reload: false });
  fake.status = { ...fake.status, granted_at: null, granted_for: null, covers: false };
  fake.status.required = true;
  fake.grant.mockReset().mockImplementation(async () => {
    fake.status = structuredClone(GRANTED) as never;
    return GRANTED;
  });
  fake.withdraw.mockReset().mockImplementation(async () => {
    fake.status = { ...fake.status, granted_at: null, granted_for: null, covers: false };
    return fake.status;
  });
  fake.propose.mockReset().mockResolvedValue({
    executable: false,
    mower_name: 'Luba',
    actions: [],
    rejected: [],
    notes: [],
    unavailable: {},
    areas: [],
  });
  fake.savePreferences.mockReset();
  consent.open = false;
});

describe('chat box', () => {
  async function ask(): Promise<void> {
    render(ChatControl, { props: { mowerId: 'mower-1' } });
    render(ConsentSheet);
    const input = await screen.findByTestId('chat-input');
    await fireEvent.input(input, { target: { value: 'send it home' } });
    await fireEvent.click(screen.getByTestId('chat-send'));
  }

  it('asks before the text is sent and sends it after agree', async () => {
    await ask();
    const body = await screen.findByTestId('consent-body');
    expect(body).toHaveTextContent(m.consent_sends_text());
    expect(body).not.toHaveTextContent(m.consent_sends_pictures());
    expect(fake.propose).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByTestId('consent-agree'));
    await waitFor(() => expect(fake.propose).toHaveBeenCalledOnce());
  });

  it('sends nothing and shows no error after not now', async () => {
    await ask();
    await fireEvent.click(await screen.findByTestId('consent-decline'));
    await waitFor(() => expect(screen.queryByTestId('consent-sheet')).toBeNull());
    expect(fake.propose).not.toHaveBeenCalled();
    expect(screen.queryByTestId('chat-error')).toBeNull();
    expect(screen.queryByTestId('chat-busy')).toBeNull();
  });

  it('does not ask when no consent is required (demo, local model)', async () => {
    fake.status.required = false;
    await ask();
    await waitFor(() => expect(fake.propose).toHaveBeenCalledOnce());
    expect(screen.queryByTestId('consent-sheet')).toBeNull();
  });
});

describe('AI provider settings', () => {
  it('shows what the user has not agreed to yet, also for the installation model', async () => {
    render(AiProviderSection);
    const state = await screen.findByTestId('ai-consent-state');
    expect(state).toHaveTextContent(
      m.consent_state_none({
        provider: 'Anthropic',
        model: 'anthropic/claude-sonnet-5-5',
      }),
    );
    expect(screen.getByTestId('ai-provider-cloud-note-effective')).toBeInTheDocument();
    expect(screen.queryByTestId('ai-consent-withdraw')).toBeNull();
  });

  it('shows provider, model and date and withdraws the agreement', async () => {
    fake.status = structuredClone(GRANTED) as never;
    render(AiProviderSection);
    const state = await screen.findByTestId('ai-consent-state');
    await waitFor(() => expect(state.dataset.covers).toBe('true'));
    expect(state).toHaveTextContent('Anthropic (anthropic/claude-sonnet-5-5)');
    await fireEvent.click(screen.getByTestId('ai-consent-withdraw'));
    await waitFor(() => expect(fake.withdraw).toHaveBeenCalledOnce());
    await waitFor(() =>
      expect(screen.getByTestId('ai-consent-state').dataset.covers).toBe('false'),
    );
  });

  it('asks before automatic analysis is switched on and leaves it off after not now', async () => {
    render(AiProviderSection);
    render(ConsentSheet);
    const toggle = await screen.findByTestId('ai-auto-switch');
    await waitFor(() => expect(toggle).not.toBeDisabled());
    await fireEvent.click(toggle);
    await fireEvent.click(await screen.findByTestId('consent-decline'));
    await waitFor(() => expect(screen.queryByTestId('consent-sheet')).toBeNull());
    expect(fake.savePreferences).not.toHaveBeenCalled();
    expect(screen.getByTestId('ai-auto-switch')).not.toBeChecked();
  });
});

// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * The chat box and the safety check: a confirmed start asks first; a declined check says
 * "Cancelled. Nothing was sent." (not "an action was refused"), sends nothing and keeps the
 * order, and a confirmed one sends the command.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const fake = vi.hoisted(() => ({
  answer: false,
  ask: vi.fn(),
  sendCommand: vi.fn(),
}));

vi.mock('#lib/safety/gate.svelte.js', () => ({ safety: { ask: fake.ask } }));
vi.mock('#lib/live.svelte.js', () => ({ sendCommand: fake.sendCommand }));
vi.mock('#lib/consent/consent.svelte.js', async (original) => ({
  ...(await original<typeof import('#lib/consent/consent.svelte.js')>()),
  consent: { guard: async (_feature: string, run: () => Promise<unknown>) => run() },
}));
vi.mock('#lib/assistant/assistant.js', async (original) => ({
  ...(await original<typeof import('#lib/assistant/assistant.js')>()),
  loadStatus: async () => ({ ready: true, interpreter: 'provider', model: 'm', hosted: true }),
  propose: async () => ({
    mower_id: 'mower-1',
    mower_name: 'Luba',
    interpreter: 'provider',
    model: 'm',
    executable: true,
    actions: [
      {
        kind: 'start_now',
        area_ids: ['front'],
        areas: [{ id: 'front', name: 'Front lawn' }],
        start_at: null,
        start_local: null,
        timezone: null,
        duration_minutes: null,
      },
    ],
    rejected: [],
    notes: [],
    unavailable: {},
    areas: [{ id: 'front', name: 'Front lawn' }],
    created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 300_000).toISOString(),
  }),
}));

import ChatControl from '#lib/components/ChatControl.svelte';
import { m } from '#lib/paraglide/messages.js';
import { setLocale } from '#lib/paraglide/runtime.js';

beforeEach(() => {
  setLocale('en', { reload: false });
  fake.ask.mockReset().mockImplementation(async () => fake.answer);
  fake.sendCommand.mockReset().mockResolvedValue(true);
});

async function confirmOrder(): Promise<void> {
  render(ChatControl, { props: { mowerId: 'mower-1' } });
  await fireEvent.input(await screen.findByTestId('chat-input'), {
    target: { value: 'mow the front lawn' },
  });
  await fireEvent.click(screen.getByTestId('chat-send'));
  await fireEvent.click(await screen.findByTestId('chat-confirm'));
}

describe('chat confirm and the safety check', () => {
  it('says cancelled, sends nothing and keeps the order when the check is declined', async () => {
    fake.answer = false;
    await confirmOrder();
    const error = await screen.findByTestId('chat-error');
    expect(error).toHaveTextContent(m.chat_cancelled());
    expect(error).not.toHaveTextContent(m.chat_failed());
    expect(fake.ask).toHaveBeenCalledWith('start_mow');
    expect(fake.sendCommand).not.toHaveBeenCalled();
    expect(screen.getByTestId('chat-confirm')).toBeInTheDocument();
  });

  it('sends the start once the check is confirmed', async () => {
    fake.answer = true;
    await confirmOrder();
    await waitFor(() => expect(fake.sendCommand).toHaveBeenCalledOnce());
    expect(screen.queryByTestId('chat-error')).toBeNull();
  });
});

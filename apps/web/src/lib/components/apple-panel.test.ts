import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const get = vi.fn();
const del = vi.fn();
vi.mock('#lib/api/client.js', () => ({
  identityApi: {
    GET: (...a: unknown[]) => get(...a),
    POST: vi.fn(),
    DELETE: (...a: unknown[]) => del(...a),
  },
  unwrap: async (p: Promise<unknown>) => p,
}));
vi.mock('$app/state', () => ({
  page: { url: new URL('https://mowdeck.test/settings#profile'), state: {} },
}));
vi.mock('$app/navigation', () => ({ replaceState: vi.fn() }));
vi.mock('#lib/auth/oauth.js', async (original) => ({
  ...(await original<typeof import('#lib/auth/oauth.js')>()),
  providerEnabled: async () => true,
}));

import { setLocale } from '#lib/paraglide/runtime.js';

import ApplePanel from './ApplePanel.svelte';

const LINK = {
  provider: 'apple',
  display_name: 'Apple ID',
  linked_at: '2026-10-25T09:00:00Z',
  last_used_at: null,
};

beforeEach(() => {
  setLocale('de', { reload: false });
  let links = [LINK];
  get.mockImplementation(async () => links);
  del.mockImplementation(async () => {
    links = [];
  });
});

afterEach(() => {
  get.mockReset();
  del.mockReset();
});

function text(element: HTMLElement): string {
  return (element.textContent ?? '').replace(/\s+/g, ' ').trim();
}

describe('ApplePanel', () => {
  it('shows a neutral label and unlinks with the note about the side of Apple', async () => {
    render(ApplePanel);
    const linked = await screen.findByTestId('apple-linked');
    // A neutral label; no e-mail address exists to show.
    expect(text(linked)).toContain('Verknüpft mit Apple');
    expect(text(linked)).not.toMatch(/@/);

    await fireEvent.click(screen.getByTestId('apple-unlink'));
    expect(text(screen.getByTestId('apple-unlink-revoke-hint'))).toBe(
      'Die Freigabe bei Apple kannst du in den Einstellungen deiner Apple-ID unter Anmeldung und Sicherheit → Mit Apple anmelden entfernen.',
    );
    await fireEvent.click(screen.getByTestId('apple-unlink-confirm'));
    await waitFor(() => expect(screen.getByTestId('apple-not-linked')).toBeTruthy());
    expect(del).toHaveBeenCalledOnce();
    expect(screen.getByTestId('apple-revoke-hint')).toBeTruthy();
  });

  it('offers to link when nothing is linked, and shows no Apple configuration', async () => {
    get.mockImplementation(async () => []);
    const { container } = render(ApplePanel);
    await screen.findByTestId('apple-not-linked');
    expect(screen.getByTestId('apple-link').textContent).toContain('Mit Apple verknüpfen');
    expect(screen.queryByTestId('apple-revoke-hint')).toBeNull();
    expect(container.textContent ?? '').not.toMatch(/services id|team id|key id|callback|\.p8/i);
  });

  it('asks for the password again before leaving for Apple', async () => {
    get.mockImplementation(async () => []);
    render(ApplePanel);
    await fireEvent.click(await screen.findByTestId('apple-link'));
    expect(screen.getByTestId('apple-link-form')).toBeTruthy();
    expect(screen.getByTestId('apple-link-continue').hasAttribute('disabled')).toBe(true);
  });

  it('speaks English as well', async () => {
    setLocale('en', { reload: false });
    render(ApplePanel);
    expect(text(await screen.findByTestId('apple-linked'))).toContain('Linked with Apple');
  });
});

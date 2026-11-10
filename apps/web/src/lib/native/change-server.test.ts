// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * The "Change server" entry (wiring only, the hand-over is tested with `NativeShell`) and the
 * language switch, in the places that show them: the component itself, the frame of the sign-in
 * pages, and the settings page, where the demo must not lock it.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { demo } from '#lib/demo/state.svelte.js';
import { setLocale } from '#lib/paraglide/runtime.js';

const { nativeShell } = vi.hoisted(() => ({
  nativeShell: {
    active: true,
    server: 'https://mowdeck.example.lan:8443' as string | null,
    platform: 'ios',
    apns: 'idle',
    apnsAvailable: false,
    widgetsAvailable: false,
    watchAvailable: false,
    watch: 'unsupported',
    widgets: 'unsupported',
    local: null,
    ntfy: false,
    changeServer: vi.fn(async () => {}),
    refreshWatch: vi.fn(async () => {}),
    refreshNtfy: vi.fn(async () => {}),
  },
}));

vi.mock('#lib/native/runtime.js', () => ({ nativeShell }));
vi.mock('./runtime', () => ({ nativeShell }));
vi.mock('#lib/config.js', async (original) => ({
  ...(await original<typeof import('#lib/config.js')>()),
  loadConfig: vi.fn(async () => (await original<typeof import('#lib/config.js')>()).DEFAULT_CONFIG),
}));

import AuthFrame from '#lib/components/AuthFrame.svelte';
import LanguageSwitch from '#lib/components/LanguageSwitch.svelte';

import ChangeServer from './ChangeServer.svelte';

const emptySnippet = createRawSnippet(() => ({ render: () => '<span></span>' }));

beforeEach(() => {
  setLocale('de', { reload: false });
  nativeShell.active = true;
  nativeShell.server = 'https://mowdeck.example.lan:8443';
  nativeShell.changeServer.mockReset().mockResolvedValue(undefined);
});

afterEach(() => {
  demo.config = null;
});

describe('ChangeServer', () => {
  it('renders nothing outside the app', () => {
    nativeShell.active = false;
    render(ChangeServer, { props: { testid: 'cs' } });
    expect(screen.queryByTestId('cs')).toBeNull();
    expect(screen.queryByTestId('cs-block')).toBeNull();
  });

  it('shows the server and asks before leaving it', async () => {
    render(ChangeServer, { props: { testid: 'cs' } });
    expect(screen.getByTestId('cs-host')).toHaveTextContent('mowdeck.example.lan:8443');
    await fireEvent.click(screen.getByTestId('cs'));
    expect(nativeShell.changeServer).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('abgemeldet');
    await fireEvent.click(screen.getByRole('button', { name: 'Abbrechen' }));
    expect(screen.getByTestId('cs')).toBeInTheDocument();
    expect(nativeShell.changeServer).not.toHaveBeenCalled();
  });

  it('switches once after the confirmation and shows it is busy meanwhile', async () => {
    let release: () => void = () => {};
    nativeShell.changeServer.mockImplementation(() => new Promise<void>((r) => (release = r)));
    render(ChangeServer, { props: { testid: 'cs' } });
    await fireEvent.click(screen.getByTestId('cs'));
    await fireEvent.click(screen.getByTestId('cs-confirm'));
    expect(nativeShell.changeServer).toHaveBeenCalledOnce();
    expect(screen.getByTestId('cs-confirm')).toBeDisabled();
    release();
    await waitFor(() => expect(screen.getByTestId('cs')).toBeInTheDocument());
  });

  it('copes with an unknown server', () => {
    nativeShell.server = null;
    render(ChangeServer, { props: { testid: 'cs' } });
    expect(screen.getByTestId('cs-host')).toHaveTextContent('keine Daten');
  });
});

describe('LanguageSwitch', () => {
  it('shows the language in use and uses the given testid', () => {
    render(LanguageSwitch, { props: { testid: 'lang' } });
    const group = screen.getByTestId('lang');
    expect(group.querySelector('[data-value="de"]')).toHaveAttribute('aria-checked', 'true');
    expect(group.querySelector('[data-value="en"]')).toHaveAttribute('aria-checked', 'false');
  });

  it('is the profile switch by default', () => {
    render(LanguageSwitch);
    expect(screen.getByTestId('language-switch')).toBeInTheDocument();
  });
});

describe('AuthFrame', () => {
  const props = { title: 'Anmelden', children: emptySnippet };

  it('offers the language switch and, in the app only, the server', () => {
    nativeShell.active = false;
    const browser = render(AuthFrame, { props });
    expect(screen.getByTestId('auth-language-switch')).toBeInTheDocument();
    expect(screen.queryByTestId('auth-change-server')).toBeNull();
    browser.unmount();

    nativeShell.active = true;
    render(AuthFrame, { props });
    expect(screen.getByTestId('auth-language-switch')).toBeInTheDocument();
    expect(screen.getByTestId('auth-change-server')).toBeEnabled();
  });
});

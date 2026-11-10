// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * What the sign-in and registration pages offer before there is a session: the language switch
 * everywhere, and in the app the way to another server, also in the demo (a signed-out user of
 * the app, or one whose demo sandbox is starting, must not be stuck on this server).
 */
import { fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DEFAULT_CONFIG, type DemoConfig } from '#lib/config.js';
import { VISITOR_KEY } from '#lib/demo/sandbox.js';

const { session, demo, nativeShell } = vi.hoisted(() => ({
  session: {
    status: 'anonymous' as 'unknown' | 'anonymous' | 'authenticated',
    ended: null as 'expired' | null,
    me: null,
    tryDemo: vi.fn(async () => {}),
    logout: vi.fn(async () => {}),
  },
  demo: {
    loaded: true,
    config: null as DemoConfig | null,
    apiExplorer: false,
    load: vi.fn(async () => null),
  },
  nativeShell: {
    active: true,
    server: 'https://mowdeck.example.lan' as string | null,
    canSignInThroughBrowser: false,
    changeServer: vi.fn(async () => {}),
  },
}));

vi.mock('$app/state', () => ({
  page: { url: new URL('https://demo.test/login'), state: {} },
}));
vi.mock('$app/navigation', () => ({ replaceState: vi.fn(), goto: vi.fn() }));
vi.mock('#lib/auth/session.svelte.js', () => ({ session, IDENTITY: '/api/v1/identity' }));
vi.mock('#lib/demo/state.svelte.js', () => ({ demo }));
vi.mock('#lib/navigate.js', () => ({ navigatePath: vi.fn(async () => {}) }));
vi.mock('#lib/native/runtime.js', () => ({ nativeShell }));
vi.mock('#lib/config.js', async (original) => ({
  ...(await original<typeof import('#lib/config.js')>()),
  loadConfig: vi.fn(async () => DEFAULT_CONFIG),
}));
vi.mock('#lib/auth/github.js', async (original) => ({
  ...(await original<typeof import('#lib/auth/github.js')>()),
  githubEnabled: vi.fn(async () => false),
}));
vi.mock('#lib/auth/apple.js', async (original) => ({
  ...(await original<typeof import('#lib/auth/apple.js')>()),
  appleEnabled: vi.fn(async () => false),
}));
vi.mock('#lib/auth/passkey.js', async (original) => ({
  ...(await original<typeof import('#lib/auth/passkey.js')>()),
  passkeysEnabled: vi.fn(async () => false),
}));

import LoginPage from './+page.svelte';
import RegisterPage from '../register/+page.svelte';

beforeEach(() => {
  localStorage.clear();
  session.status = 'anonymous';
  session.tryDemo.mockReset().mockImplementation(async () => {});
  nativeShell.active = true;
  nativeShell.changeServer.mockClear();
  demo.config = null;
});

afterEach(() => {
  localStorage.clear();
});

const pages = [
  ['login', LoginPage],
  ['register', RegisterPage],
] as const;

describe.each(pages)('%s page', (_name, Page) => {
  it('offers the language switch and the server in the app (normal installation)', () => {
    render(Page);
    expect(screen.getByTestId('auth-language-switch')).toBeInTheDocument();
    expect(screen.getByTestId('auth-change-server')).toBeEnabled();
    expect(screen.getByTestId('auth-change-server-host')).toHaveTextContent('mowdeck.example.lan');
  });

  it('offers them in the demo as well', () => {
    demo.config = { resetHours: 6 };
    render(Page);
    expect(screen.getByTestId('auth-language-switch')).toBeInTheDocument();
    expect(screen.getByTestId('auth-change-server')).toBeEnabled();
  });

  it('offers only the language switch in a browser', () => {
    nativeShell.active = false;
    render(Page);
    expect(screen.getByTestId('auth-language-switch')).toBeInTheDocument();
    expect(screen.queryByTestId('auth-change-server')).toBeNull();
  });
});

describe('login page in the demo while a sandbox starts', () => {
  it('keeps the entry usable and switches after the confirmation', async () => {
    demo.config = { resetHours: 6, sandbox: { idleMinutes: 30 } };
    localStorage.setItem(VISITOR_KEY, '1');
    session.tryDemo.mockImplementation(() => new Promise<void>(() => {}));
    render(LoginPage);
    await screen.findByTestId('login-demo-recreating');
    await fireEvent.click(screen.getByTestId('auth-change-server'));
    await fireEvent.click(screen.getByTestId('auth-change-server-confirm'));
    expect(nativeShell.changeServer).toHaveBeenCalledOnce();
  });
});

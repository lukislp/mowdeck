/**
 * The demo entry of the sign-in page (ADR 0068) with demo sandboxes (ADR 0076): the note, the
 * visitor flag, a fresh sandbox for a visitor whose sandbox is gone, "the demo is full" with its
 * countdown and automatic retry, and the calm 429 message.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiProblem } from '#lib/api/problem.js';
import { DEFAULT_CONFIG, type DemoConfig } from '#lib/config.js';
import { DEMO_FULL_PROBLEM, TOO_MANY_PROBLEM, VISITOR_KEY } from '#lib/demo/sandbox.js';

const { session, demo, navigatePath, url } = vi.hoisted(() => ({
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
  navigatePath: vi.fn(async () => {}),
  url: { current: new URL('https://demo.test/login?next=%2Fschedules') },
}));

vi.mock('$app/state', () => ({
  page: {
    get url() {
      return url.current;
    },
    state: {},
  },
}));
vi.mock('$app/navigation', () => ({ replaceState: vi.fn(), goto: vi.fn() }));
vi.mock('#lib/auth/session.svelte.js', () => ({ session, IDENTITY: '/api/v1/identity' }));
vi.mock('#lib/demo/state.svelte.js', () => ({ demo }));
vi.mock('#lib/navigate.js', () => ({ navigatePath }));
vi.mock('#lib/native/runtime.js', () => ({
  nativeShell: { active: false, canSignInThroughBrowser: false },
}));
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

const SANDBOX: DemoConfig = { resetHours: 6, sandbox: { idleMinutes: 30 } };

function full(seconds: number | null): ApiProblem {
  return new ApiProblem({
    type: DEMO_FULL_PROBLEM,
    status: 503,
    title: 'The demo is full',
    retryAfter: seconds,
  });
}

beforeEach(() => {
  localStorage.clear();
  session.status = 'anonymous';
  session.tryDemo.mockReset();
  session.tryDemo.mockImplementation(async () => {});
  navigatePath.mockClear();
  demo.config = SANDBOX;
  url.current = new URL('https://demo.test/login?next=%2Fschedules');
});

afterEach(() => {
  localStorage.clear();
});

describe('demo entry with sandboxes', () => {
  it('explains the own sandbox, its deletion and the reset', () => {
    render(LoginPage);
    expect(screen.getByTestId('login-demo')).toHaveAttribute('data-mode', 'sandbox');
    const note = screen.getByTestId('login-demo-note');
    expect(note).toHaveTextContent(/sandbox/i);
    expect(note).toHaveTextContent(/30 min/);
    expect(note).toHaveTextContent(/6 h/);
    expect(note).not.toHaveTextContent(/teilen|shares/i);
    // A browser that never started a demo is not started by itself.
    expect(session.tryDemo).not.toHaveBeenCalled();
  });

  it('keeps the shared note without sandboxes', () => {
    demo.config = { resetHours: 6 };
    localStorage.setItem(VISITOR_KEY, '1');
    render(LoginPage);
    expect(screen.getByTestId('login-demo')).toHaveAttribute('data-mode', 'shared');
    expect(screen.getByTestId('login-demo-note')).toHaveTextContent(/teilen|shares/i);
    // The shared demo never starts by itself, visitor or not.
    expect(session.tryDemo).not.toHaveBeenCalled();
  });

  it('marks the browser as a visitor and continues to next', async () => {
    render(LoginPage);
    await fireEvent.click(screen.getByTestId('login-demo-start'));
    await waitFor(() => expect(navigatePath).toHaveBeenCalledWith('/schedules', { replace: true }));
    expect(session.tryDemo).toHaveBeenCalledOnce();
    expect(localStorage.getItem(VISITOR_KEY)).toBe('1');
  });

  it('starts a fresh sandbox by itself for a visitor whose sandbox is gone', async () => {
    localStorage.setItem(VISITOR_KEY, '1');
    let release: () => void = () => {};
    session.tryDemo.mockImplementation(() => new Promise<void>((resolve) => (release = resolve)));
    render(LoginPage);
    const status = await screen.findByTestId('login-demo-recreating');
    expect(status).toHaveAttribute('role', 'status');
    expect(session.tryDemo).toHaveBeenCalledOnce();
    release();
    await waitFor(() => expect(navigatePath).toHaveBeenCalledWith('/schedules', { replace: true }));
    expect(session.tryDemo).toHaveBeenCalledOnce();
  });

  it('does not start by itself while the session is still being restored', () => {
    localStorage.setItem(VISITOR_KEY, '1');
    session.status = 'unknown';
    render(LoginPage);
    expect(session.tryDemo).not.toHaveBeenCalled();
  });

  it('never starts by itself for the app hand-off', () => {
    localStorage.setItem(VISITOR_KEY, '1');
    url.current = new URL(
      'https://demo.test/login?app_request=0f8fad5b-d9cb-469f-a165-70867728950e&app_state=state-of-the-app-1234',
    );
    render(LoginPage);
    expect(session.tryDemo).not.toHaveBeenCalled();
  });

  it('shows the full demo with a countdown and tries again at zero', async () => {
    session.tryDemo.mockRejectedValueOnce(full(1));
    render(LoginPage);
    await fireEvent.click(screen.getByTestId('login-demo-start'));
    const state = await screen.findByTestId('login-demo-full');
    expect(state).toHaveTextContent(/voll|full/i);
    expect(screen.getByTestId('login-demo-countdown')).toHaveTextContent('00:01');
    // The heading takes the focus (the button the visitor used is gone).
    await waitFor(() => expect(document.activeElement).toBe(state.querySelector('h2')));
    expect(state.querySelector('[role="status"]')).not.toBeNull();
    await waitFor(() => expect(session.tryDemo).toHaveBeenCalledTimes(2), { timeout: 3_000 });
    await waitFor(() => expect(navigatePath).toHaveBeenCalledWith('/schedules', { replace: true }));
    expect(localStorage.getItem(VISITOR_KEY)).toBe('1');
  });

  it('falls back to two minutes and can try again right away', async () => {
    session.tryDemo.mockRejectedValueOnce(full(null)).mockRejectedValueOnce(full(5));
    render(LoginPage);
    await fireEvent.click(screen.getByTestId('login-demo-start'));
    expect(await screen.findByTestId('login-demo-countdown')).toHaveTextContent('02:00');
    await fireEvent.click(screen.getByTestId('login-demo-retry'));
    // Still full: a new countdown from the new Retry-After.
    await waitFor(() =>
      expect(screen.getByTestId('login-demo-countdown')).toHaveTextContent(/00:0[45]/),
    );
    expect(session.tryDemo).toHaveBeenCalledTimes(2);
  });

  it('says how long to wait after too many sandboxes, without retrying', async () => {
    session.tryDemo.mockRejectedValueOnce(
      new ApiProblem({ type: TOO_MANY_PROBLEM, status: 429, retryAfter: 600 }),
    );
    render(LoginPage);
    await fireEvent.click(screen.getByTestId('login-demo-start'));
    const busy = await screen.findByTestId('login-demo-busy');
    expect(busy).toHaveTextContent(/10 min/);
    expect(screen.queryByTestId('login-demo-full')).toBeNull();
    await new Promise((resolve) => setTimeout(resolve, 1_200));
    expect(session.tryDemo).toHaveBeenCalledOnce();
  });
});

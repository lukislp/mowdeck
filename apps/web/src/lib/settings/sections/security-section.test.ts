import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const get = vi.fn();
const put = vi.fn();
const post = vi.fn();
const del = vi.fn();
const location = vi.hoisted(() => ({ url: new URL('https://mowdeck.test/settings#security') }));
vi.mock('#lib/api/client.js', () => ({
  identityApi: {
    GET: (...a: unknown[]) => get(...a),
    PUT: (...a: unknown[]) => put(...a),
    POST: (...a: unknown[]) => post(...a),
    DELETE: (...a: unknown[]) => del(...a),
  },
  unwrap: async (p: Promise<unknown>) => p,
}));
vi.mock('$app/state', () => ({
  page: {
    get url() {
      return location.url;
    },
    state: {},
  },
}));
vi.mock('$app/navigation', () => ({ goto: vi.fn(), replaceState: vi.fn() }));
// The passkey and provider panels have their own tests.
vi.mock('#lib/components/PasskeysPanel.svelte', async () => ({
  default: (await import('#lib/components/SlowLoading.svelte')).default,
}));
vi.mock('#lib/components/LoginLinksPanel.svelte', async () => ({
  default: (await import('#lib/components/SlowLoading.svelte')).default,
}));

import { setLocale } from '#lib/paraglide/runtime.js';

import SecuritySection from './SecuritySection.svelte';

const CURRENT = '11111111-1111-4111-8111-111111111111';
const STRANGER = '22222222-2222-4222-8222-222222222222';
const NOW = Date.now();

function session(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    account_id: '00000000-0000-4000-8000-000000000001',
    current: false,
    client: 'browser',
    browser: 'Firefox',
    os: 'Windows',
    label: 'Firefox on Windows',
    ip_prefix: '8.8.4.x',
    network: 'public',
    created_at: new Date(NOW - 3_600_000).toISOString(),
    last_used_at: new Date(NOW - 60_000).toISOString(),
    expires_at: new Date(NOW + 86_400_000).toISOString(),
    method: 'pwd',
    ...overrides,
  };
}

let sessions: unknown[];
let overview: Record<string, unknown>;

beforeEach(() => {
  setLocale('de', { reload: false });
  location.url = new URL('https://mowdeck.test/settings#security');
  sessions = [
    session(CURRENT, { current: true, client: 'app', browser: null, os: 'iOS' }),
    session(STRANGER),
  ];
  overview = {
    password_set: true,
    password_changed_at: '2026-10-01T09:00:00Z',
    must_change_password: false,
    new_login_alerts: true,
    passkeys: 2,
    providers: ['github'],
    personal_tokens: 1,
    sessions: 2,
    known_devices: 3,
  };
  get.mockImplementation(async (path: string) => {
    switch (path) {
      case '/api/v1/identity/me/security':
        return overview;
      case '/api/v1/identity/me/sessions':
        return sessions;
      case '/api/v1/identity/me/security-events':
        return [
          {
            id: 2,
            at: new Date(NOW - 120_000).toISOString(),
            action: 'security.new_device',
            outcome: 'success',
            target_type: 'user',
            details: { client: 'browser', browser: 'Firefox', os: 'Windows' },
            ip_prefix: '8.8.4.x',
            network: 'public',
          },
          {
            id: 1,
            at: new Date(NOW - 180_000).toISOString(),
            action: 'session.login_failed',
            outcome: 'failure',
            target_type: 'user',
            details: {},
            ip_prefix: '192.168.1.x',
            network: 'local',
          },
        ];
      case '/api/v1/identity/me/api-tokens':
        return [
          {
            id: 'tok-1',
            name: 'Regen-Skript',
            prefix: 'mdk_abcd',
            scopes: ['mower.view'],
            level: { type: 'account', id: 'x' },
            created_at: new Date(NOW - 86_400_000).toISOString(),
            expires_at: new Date(NOW + 30 * 86_400_000).toISOString(),
            last_used_at: null,
            revoked_at: null,
          },
        ];
      default:
        throw new Error(`unexpected GET ${path}`);
    }
  });
  del.mockImplementation(
    async (_path: string, init: { params: { path: { session_id: string } } }) => {
      sessions = (sessions as { id: string }[]).filter(
        (s) => s.id !== init?.params?.path?.session_id,
      );
    },
  );
  put.mockImplementation(async (_path: string, init: { body: { new_login_alerts: boolean } }) => ({
    new_login_alerts: init.body.new_login_alerts,
  }));
});

afterEach(() => {
  get.mockReset();
  put.mockReset();
  post.mockReset();
  del.mockReset();
});

describe('SecuritySection', () => {
  it('shows the gauges, the devices with the current one marked, tokens and events', async () => {
    render(SecuritySection);
    const posture = await screen.findByTestId('security-posture');
    expect(within(posture).getByTestId('security-gauge-passkeys').textContent).toContain('2');
    expect(within(posture).getByTestId('security-gauge-providers').textContent).toContain('GitHub');
    const current = screen.getByTestId('session-current');
    expect(current.textContent).toContain('MowDeck-App auf iOS');
    expect(current.textContent).toContain('Dieses Gerät');
    expect(screen.getByTestId(`session-${STRANGER}`).textContent).toContain('Firefox auf Windows');
    expect(screen.getByTestId('security-token-tok-1').textContent).toContain('Regen-Skript');
    expect(screen.getByTestId('security-event-security.new_device').textContent).toContain(
      'Neues Gerät: Firefox auf Windows',
    );
    expect(screen.getByTestId('security-event-session.login_failed').className).toContain(
      'tone-alert',
    );
  });

  it('signs another device out only after a second tap', async () => {
    render(SecuritySection);
    await fireEvent.click(await screen.findByTestId(`session-revoke-${STRANGER}`));
    expect(del).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByTestId('session-revoke-confirm'));
    await waitFor(() => expect(screen.queryByTestId(`session-${STRANGER}`)).toBeNull());
    expect(del.mock.calls[0]![0]).toBe('/api/v1/identity/me/sessions/{session_id}');
  });

  it('flags the session a notification links to', async () => {
    location.url = new URL(`https://mowdeck.test/settings?revoke=${STRANGER}#security`);
    render(SecuritySection);
    const flagged = await screen.findByTestId('security-flagged');
    expect(flagged.textContent).toContain('Neue Anmeldung von Firefox auf Windows');
    await fireEvent.click(within(flagged).getByTestId('security-flagged-revoke'));
    await waitFor(() => expect(del).toHaveBeenCalledTimes(1));
  });

  it('turns the alerts off through the API', async () => {
    render(SecuritySection);
    await fireEvent.click(await screen.findByTestId('security-alerts-switch'));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/v1/identity/me/security/alerts', {
        body: { new_login_alerts: false },
      }),
    );
  });
});

import { beforeEach, describe, expect, it } from 'vitest';

import { setLocale } from '#lib/paraglide/runtime.js';
import { builtIn } from '#lib/shell/registry.svelte.js';

import { forwardTarget } from './sections';
import {
  deviceIcon,
  deviceText,
  eventTitle,
  eventTone,
  groupByDay,
  methodText,
  orderSessions,
  revokeTarget,
  whereText,
  type SecurityEvent,
  type SessionInfo,
} from './security';

beforeEach(() => setLocale('de', { reload: false }));

const SID = '6f1c1d2e-3a4b-4c5d-8e9f-0a1b2c3d4e5f';

function session(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    id: SID,
    account_id: '00000000-0000-4000-8000-000000000001',
    current: false,
    client: 'browser',
    browser: 'Firefox',
    os: 'Windows',
    label: 'Firefox on Windows',
    ip_prefix: '8.8.4.x',
    network: 'public',
    created_at: '2026-10-28T08:00:00Z',
    last_used_at: '2026-10-29T08:00:00Z',
    expires_at: '2026-11-05T08:00:00Z',
    method: 'pwd',
    ...overrides,
  };
}

function event(overrides: Partial<SecurityEvent> = {}): SecurityEvent {
  return {
    id: 1,
    at: '2026-10-29T08:00:00Z',
    action: 'session.login',
    outcome: 'success',
    target_type: 'user',
    details: {},
    ip_prefix: '8.8.4.x',
    network: 'public',
    ...overrides,
  };
}

describe('security section texts', () => {
  it('names devices in the app language from the parts Identity sends', () => {
    expect(deviceText(session())).toBe('Firefox auf Windows');
    expect(deviceText(session({ client: 'app', browser: null, os: 'iOS' }))).toBe(
      'MowDeck-App auf iOS',
    );
    expect(deviceText(session({ client: 'script', browser: 'curl', os: null }))).toBe(
      'Skript (curl)',
    );
    expect(deviceText(session({ client: 'unknown', browser: null, os: null, label: '' }))).toBe(
      'Unbekanntes Gerät',
    );
    setLocale('en', { reload: false });
    expect(deviceText(session())).toBe('Firefox on Windows');
  });

  it('shows the network, never more than the prefix', () => {
    expect(whereText('192.168.1.x', 'local')).toBe('Heimnetz · 192.168.1.x');
    expect(whereText(null, 'local')).toBe('Heimnetz');
    expect(whereText('8.8.4.x', 'public')).toBe('8.8.4.x');
    expect(whereText(null, 'unknown')).toBe('Adresse unbekannt');
  });

  it('names the sign-in methods', () => {
    expect(methodText('pwd')).toBe('Passwort');
    expect(methodText('passkey')).toBe('Passkey');
    expect(methodText('github')).toBe('GitHub');
    expect(methodText(null)).toBeNull();
    expect(deviceIcon('app')).toBe('phone');
    expect(deviceIcon('browser')).toBe('globe');
  });

  it('turns audit actions into lines and tones', () => {
    expect(eventTitle(event({ details: { method: 'passkey' } }))).toBe('Angemeldet mit Passkey');
    expect(
      eventTitle(
        event({
          action: 'security.new_device',
          details: { client: 'browser', browser: 'Safari', os: 'macOS', device: 'x' },
        }),
      ),
    ).toBe('Neues Gerät: Safari auf macOS');
    expect(eventTitle(event({ action: 'session.revoked_others', details: { sessions: 2 } }))).toBe(
      'Auf 2 anderen Geräten abgemeldet',
    );
    expect(eventTitle(event({ action: 'session.revoked', details: { current: true } }))).toBe(
      'Abgemeldet',
    );
    expect(
      eventTitle(event({ action: 'external_identity.linked', details: { provider: 'github' } })),
    ).toBe('GitHub verknüpft');
    expect(eventTitle(event({ action: 'something.new' }))).toBe('something.new');
    expect(eventTone(event({ action: 'session.login_failed', outcome: 'failure' }))).toBe('alert');
    expect(eventTone(event({ action: 'security.new_device' }))).toBe('notice');
    expect(eventTone(event())).toBe('ok');
  });

  it('groups events by local day, newest first', () => {
    const days = groupByDay([
      event({ id: 3, at: '2026-10-29T10:00:00' }),
      event({ id: 2, at: '2026-10-29T09:00:00' }),
      event({ id: 1, at: '2026-10-27T09:00:00' }),
    ]);
    expect(days.map((d) => [d.day, d.events.map((e) => e.id)])).toEqual([
      ['2026-10-29', [3, 2]],
      ['2026-10-27', [1]],
    ]);
  });

  it('puts the current session first, then by last use', () => {
    const ordered = orderSessions([
      session({ id: 'a', last_used_at: '2026-10-29T07:00:00Z' }),
      session({ id: 'b', last_used_at: '2026-10-29T09:00:00Z' }),
      session({ id: 'c', current: true, last_used_at: '2026-10-28T09:00:00Z' }),
    ]);
    expect(ordered.map((s) => s.id)).toEqual(['c', 'b', 'a']);
  });

  it('reads the session a notification asked to end', () => {
    expect(revokeTarget(new URLSearchParams(`revoke=${SID.toUpperCase()}`))).toBe(SID);
    expect(revokeTarget(new URLSearchParams('revoke=../../etc'))).toBeNull();
    expect(revokeTarget(new URLSearchParams(''))).toBeNull();
  });

  it('forwards the notification link to the security section and keeps the session', () => {
    expect(forwardTarget(builtIn, '/settings/security', `?revoke=${SID}`)).toBe(
      `/settings?revoke=${SID}#security`,
    );
  });
});

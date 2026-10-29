/**
 * The security section (ADR 0065): the signed-in user's sessions and devices, security events
 * and alert setting under `/api/v1/identity/me/...`. Identity describes a device by its user
 * agent (client kind, browser family, operating system, never versions) and shortens addresses
 * to their network; the texts here are built from those parts so they follow the app language.
 */
import type { components } from '#lib/generated/identity.js';
import { m } from '#lib/paraglide/messages.js';

export type SessionInfo = components['schemas']['SessionOut'];
export type SecurityEvent = components['schemas']['SecurityEventOut'];
export type SecurityOverview = components['schemas']['SecurityOverviewOut'];

/** The period of the event list (Identity's default). */
export const EVENT_DAYS = 30;

/** The query parameter of the "this wasn't me" link in the new-login notification. */
export const REVOKE_PARAM = 'revoke';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The session a notification asked to end (`?revoke=<id>`), or null. */
export function revokeTarget(search: Pick<URLSearchParams, 'get'>): string | null {
  const value = search.get(REVOKE_PARAM);
  return value && UUID.test(value) ? value.toLowerCase() : null;
}

interface DeviceParts {
  client?: string | null;
  browser?: string | null;
  os?: string | null;
  label?: string | null;
}

/** "Firefox auf Windows", "MowDeck-App auf iOS", "Skript (curl)". */
export function deviceText(device: DeviceParts): string {
  const { client, browser, os } = device;
  if (client === 'app') return os ? m.sec_device_app_on({ os }) : m.sec_device_app();
  if (client === 'script') {
    return browser ? m.sec_device_script({ name: browser }) : m.sec_device_unknown();
  }
  if (browser && os) return m.sec_device_browser_on({ browser, os });
  return browser || os || device.label || m.sec_device_unknown();
}

/** The icon of a device kind. */
export function deviceIcon(client: string | null | undefined): 'phone' | 'globe' | 'list' {
  if (client === 'app') return 'phone';
  if (client === 'script') return 'list';
  return 'globe';
}

/** Where a session came from: "Heimnetz · 192.168.1.x", "8.8.4.x", "Adresse unbekannt". */
export function whereText(prefix: string | null | undefined, network: string | null): string {
  if (network === 'local')
    return prefix ? m.sec_where_local_prefix({ prefix }) : m.sec_where_local();
  return prefix || m.sec_where_unknown();
}

/** How a session was started, or null when Identity does not know it. */
export function methodText(method: string | null | undefined): string | null {
  switch (method) {
    case 'pwd':
    case 'password':
      return m.sec_method_password();
    case 'passkey':
      return m.sec_method_passkey();
    case 'github':
      return 'GitHub';
    case 'apple':
      return 'Apple';
    case 'app_handoff':
      return m.sec_method_app();
    default:
      return method || null;
  }
}

export type EventTone = 'ok' | 'notice' | 'alert';

/** Failures are alerts; new devices and ended or removed things deserve a look. */
export function eventTone(event: Pick<SecurityEvent, 'action' | 'outcome'>): EventTone {
  if (event.outcome !== 'success') return 'alert';
  switch (event.action) {
    case 'security.new_device':
    case 'session.refresh_reuse':
    case 'session.revoked':
    case 'session.revoked_others':
    case 'session.logout_all':
    case 'passkey.removed':
    case 'external_identity.unlinked':
    case 'api_token.revoked':
      return 'notice';
    default:
      return 'ok';
  }
}

function text(details: Record<string, unknown>, key: string): string {
  const value = details[key];
  return typeof value === 'string' || typeof value === 'number' ? String(value) : '';
}

function providerText(details: Record<string, unknown>): string {
  const provider = text(details, 'provider');
  return provider === 'github' ? 'GitHub' : provider === 'apple' ? 'Apple' : provider;
}

/** One line per event, in the app language. Unknown actions show their name. */
export function eventTitle(event: Pick<SecurityEvent, 'action' | 'details'>): string {
  const details = event.details ?? {};
  switch (event.action) {
    case 'session.login': {
      const method = methodText(text(details, 'method'));
      return method ? m.sec_ev_login_with({ method }) : m.sec_ev_login();
    }
    case 'session.login_failed':
      return m.sec_ev_login_failed();
    case 'session.logout_all':
      return m.sec_ev_logout_all();
    case 'session.refresh_reuse':
      return m.sec_ev_refresh_reuse();
    case 'session.revoked':
      return details['current'] === true ? m.sec_ev_signed_out() : m.sec_ev_session_revoked();
    case 'session.revoked_others':
      return m.sec_ev_revoked_others({ count: Number(details['sessions'] ?? 0) });
    case 'security.new_device':
      return m.sec_ev_new_device({
        device: deviceText({
          client: text(details, 'client'),
          browser: text(details, 'browser'),
          os: text(details, 'os'),
          label: text(details, 'device'),
        }),
      });
    case 'security.alerts_changed':
      return details['new_login_alerts'] === false ? m.sec_ev_alerts_off() : m.sec_ev_alerts_on();
    case 'user.password_changed':
      return m.sec_ev_password_changed();
    case 'user.password_reset':
      return m.sec_ev_password_reset();
    case 'passkey.added':
      return m.sec_ev_passkey_added({ name: text(details, 'name') });
    case 'passkey.renamed':
      return m.sec_ev_passkey_renamed({ name: text(details, 'to') });
    case 'passkey.removed':
      return m.sec_ev_passkey_removed({ name: text(details, 'name') });
    case 'external_identity.linked':
      return m.sec_ev_provider_linked({ provider: providerText(details) });
    case 'external_identity.unlinked':
      return m.sec_ev_provider_unlinked({ provider: providerText(details) });
    case 'passkey.reauth_failed':
    case 'external_identity.reauth_failed':
    case 'step_up.reauth_failed':
    case 'step_up.failed':
      return m.sec_ev_confirm_failed();
    case 'api_token.personal_created':
      return m.sec_ev_pat_created();
    case 'api_token.created':
    case 'api_token.widget_created':
      return m.sec_ev_token_created();
    case 'api_token.revoked':
      return m.sec_ev_token_revoked();
    default:
      return event.action;
  }
}

export interface EventDay {
  /** `YYYY-MM-DD` in local time. */
  day: string;
  events: SecurityEvent[];
}

function localDay(at: string): string {
  const date = new Date(at);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** The events (newest first) grouped by local day, newest day first. */
export function groupByDay(events: readonly SecurityEvent[]): EventDay[] {
  const days: EventDay[] = [];
  for (const event of events) {
    const day = localDay(event.at);
    const last = days[days.length - 1];
    if (last && last.day === day) last.events.push(event);
    else days.push({ day, events: [event] });
  }
  return days;
}

/** The current session first, then the others by last use (the server's order, kept stable). */
export function orderSessions(sessions: readonly SessionInfo[]): SessionInfo[] {
  return [...sessions].sort((a, b) => {
    if (a.current !== b.current) return a.current ? -1 : 1;
    return Date.parse(b.last_used_at) - Date.parse(a.last_used_at);
  });
}

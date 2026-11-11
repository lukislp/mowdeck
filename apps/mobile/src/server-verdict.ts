// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * The rule of the first-launch screen: which servers the app opens. Pure, no DOM and no network:
 * `decideServer` turns what the probe saw into one verdict, `connect.ts` does the probing and
 * `launcher.ts` only renders the verdict.
 *
 * - A server that gives the MowDeck signal (its web app manifest) is opened.
 * - A server that answers without the signal (another website, a login portal, a Wi-Fi sign-in
 *   page, an error page) is refused for good: the app loads MowDeck servers only.
 * - A server that cannot be reached right now (no connection, DNS, TLS, timeout, a proxy's
 *   502/503/504) may be opened "anyway" after a confirmation that names the host, because owners
 *   legitimately set the app up away from home (LAN, VPN, server down).
 * - A redirect to another host counts only when that host gives the signal, and then the user
 *   confirms the new host.
 */
import { isMowDeckManifest, normalizeServerUrl } from './server-url';

/** What the probe saw: a failed request, or a response (with the final URL after redirects). */
export type ProbeResult =
  | { kind: 'failed' }
  | { kind: 'response'; status: number; data: unknown; url?: string; location?: string };

/** The address the user typed, as `normalizeServerUrl` returned it. */
export interface RequestedServer {
  origin: string;
  insecure: boolean;
}

export type ServerVerdict =
  /** A MowDeck server: open it. */
  | { kind: 'connect'; origin: string; insecure: boolean }
  /** A MowDeck server on another host than typed: open it after the user confirmed the host. */
  | { kind: 'redirected'; origin: string; insecure: boolean; host: string }
  /** http:// answered with a redirect to https:// on the same host: probe that address once. */
  | { kind: 'upgrade'; origin: string }
  /** Answers, but is not a MowDeck server: refused, no way around. */
  | { kind: 'not-mowdeck'; origin: string }
  /** Not reachable from here right now: may be opened after a confirmation naming the host. */
  | { kind: 'unreachable'; origin: string; insecure: boolean };

/** Statuses a proxy or CDN answers when the server behind it is down. */
const SERVER_DOWN = new Set([502, 503, 504, 521, 522, 523, 524]);

function sameHostname(a: string, b: string): boolean {
  return new URL(a).hostname.toLowerCase() === new URL(b).hostname.toLowerCase();
}

/** The origin the response came from (the final URL after redirects), null when unreadable. */
function finalOrigin(requested: RequestedServer, url: string | undefined): string | null {
  if (!url) return requested.origin;
  try {
    const { protocol, host } = new URL(url);
    return `${protocol}//${host}`;
  } catch {
    return null;
  }
}

export function decideServer(requested: RequestedServer, probe: ProbeResult): ServerVerdict {
  const { origin, insecure } = requested;
  if (probe.kind === 'failed' || SERVER_DOWN.has(probe.status)) {
    return { kind: 'unreachable', origin, insecure };
  }
  if (probe.status >= 300 && probe.status < 400) {
    const upgrade = httpsUpgrade(origin, probe.location);
    return upgrade ? { kind: 'upgrade', origin: upgrade } : { kind: 'not-mowdeck', origin };
  }
  if (probe.status !== 200 || !isMowDeckManifest(probe.data)) {
    return { kind: 'not-mowdeck', origin };
  }
  const final = finalOrigin(requested, probe.url);
  if (final === null) return { kind: 'not-mowdeck', origin };
  // A downgrade from https to http would drop the transport the user asked for.
  if (origin.startsWith('https:') && final.startsWith('http:')) {
    return { kind: 'not-mowdeck', origin };
  }
  const checked = normalizeServerUrl(final);
  if (!checked.ok) return { kind: 'not-mowdeck', origin };
  if (!sameHostname(origin, checked.origin)) {
    return {
      kind: 'redirected',
      origin: checked.origin,
      insecure: checked.insecure,
      host: new URL(checked.origin).host,
    };
  }
  return { kind: 'connect', origin: checked.origin, insecure: checked.insecure };
}

/** The https:// origin a redirect of an http:// address points to on the same host, if any. */
function httpsUpgrade(origin: string, location: string | undefined): string | null {
  if (!origin.startsWith('http:') || !location) return null;
  try {
    const target = new URL(location, origin);
    if (target.protocol !== 'https:' || !sameHostname(origin, target.origin)) return null;
    return target.origin;
  } catch {
    return null;
  }
}

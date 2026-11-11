// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * The first-launch flow without the DOM: validate the typed address, probe the server for its
 * web app manifest, and let the pure rule (`server-verdict.ts`) decide. The request goes through
 * Capacitor's native HTTP (the bundled page has another origin, so a browser fetch would be
 * blocked by CORS); it is injected for the tests.
 */
import { normalizeServerUrl, type ServerUrlProblem } from './server-url';
import { decideServer, type ProbeResult, type ServerVerdict } from './server-verdict';

export type HttpGet = (
  url: string,
) => Promise<{ status: number; data: unknown; url?: string; location?: string }>;

export type ConnectResult =
  { kind: 'invalid'; problem: ServerUrlProblem } | Exclude<ServerVerdict, { kind: 'upgrade' }>;

/** The one network call of the check: a request that fails is a result, not an exception. */
export async function probeServer(origin: string, get: HttpGet): Promise<ProbeResult> {
  try {
    const response = await get(`${origin}/manifest.webmanifest`);
    return { kind: 'response', ...response };
  } catch {
    // DNS, TLS (an untrusted certificate), a refused connection or a timeout
    return { kind: 'failed' };
  }
}

export async function checkServer(input: string, get: HttpGet): Promise<ConnectResult> {
  const parsed = normalizeServerUrl(input);
  if (!parsed.ok) return { kind: 'invalid', problem: parsed.problem };
  let requested = { origin: parsed.origin, insecure: parsed.insecure };
  let verdict = decideServer(requested, await probeServer(requested.origin, get));
  if (verdict.kind === 'upgrade') {
    // http:// redirected to https:// on the same host (a client that does not follow it): once
    requested = { origin: verdict.origin, insecure: false };
    verdict = decideServer(requested, await probeServer(requested.origin, get));
  }
  return verdict.kind === 'upgrade' ? { kind: 'not-mowdeck', origin: requested.origin } : verdict;
}

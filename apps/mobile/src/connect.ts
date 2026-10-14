/**
 * The first-launch flow without the DOM: validate the typed address, then ask the server for
 * its web app manifest to make sure a MowDeck server answers there. The request goes through
 * Capacitor's native HTTP (the bundled page has another origin, so a browser fetch would be
 * blocked by CORS); it is injected for the tests.
 */
import { isMowDeckManifest, normalizeServerUrl, type ServerUrlProblem } from './server-url';

export type HttpGet = (url: string) => Promise<{ status: number; data: unknown }>;

export type ConnectResult =
  | { kind: 'invalid'; problem: ServerUrlProblem }
  | { kind: 'ok'; origin: string; insecure: boolean }
  | { kind: 'unreachable'; origin: string; insecure: boolean };

export async function checkServer(input: string, get: HttpGet): Promise<ConnectResult> {
  const parsed = normalizeServerUrl(input);
  if (!parsed.ok) return { kind: 'invalid', problem: parsed.problem };
  const { origin, insecure } = parsed;
  try {
    const response = await get(`${origin}/manifest.webmanifest`);
    if (response.status === 200 && isMowDeckManifest(response.data)) {
      return { kind: 'ok', origin, insecure };
    }
  } catch {
    // DNS, TLS (an untrusted certificate) or a timeout: reported as unreachable
  }
  return { kind: 'unreachable', origin, insecure };
}

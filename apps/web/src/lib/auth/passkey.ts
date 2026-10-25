/**
 * Passkey ceremonies of the web app (ADR 0050).
 *
 * - Login: `POST /passkeys/challenge` gives the options and a ceremony id, the browser asks the
 *   authenticator (no username: discoverable credentials), `POST /passkeys/session` starts the
 *   same session as a password login (through {@link SessionManager.signIn}).
 * - Conditional UI: the same login with `mediation: 'conditional'`, started when the login page
 *   opens; the browser then offers the passkeys in the username field's autofill.
 * - Registration: the current password again, then `navigator.credentials.create()`.
 */
import { problemFromResponse } from '#lib/api/problem.js';

import { IDENTITY, type SessionManager } from './session.svelte';
import { credentialJson, creationOptions, requestOptions } from './webauthn';

interface Options {
  ceremony_id: string;
  public_key: Record<string, unknown>;
}

export interface PasskeyDeps {
  fetch: typeof fetch;
  credentials: Pick<CredentialsContainer, 'get' | 'create'>;
}

function browserDeps(): PasskeyDeps {
  return {
    fetch: (...args) => globalThis.fetch(...args),
    credentials: navigator.credentials,
  };
}

/** The login methods of the installation (`GET /login-methods`); never rejects. */
export async function passkeysEnabled(
  fetchFn: typeof fetch = (...args) => globalThis.fetch(...args),
): Promise<boolean> {
  try {
    const response = await fetchFn(`${IDENTITY}/login-methods`, { credentials: 'omit' });
    if (!response.ok) return false;
    const body = (await response.json()) as { passkey?: unknown };
    return body.passkey === true;
  } catch {
    return false;
  }
}

/**
 * A passwordless login. Resolves when the session is up (`/me` loaded); rejects with an
 * {@link ApiProblem} from Identity or the browser's `DOMException` (`NotAllowedError` when the
 * user cancels, `AbortError` when `signal` fires).
 */
export async function signInWithPasskey(
  session: Pick<SessionManager, 'signIn'>,
  options: { mediation?: CredentialMediationRequirement; signal?: AbortSignal } = {},
  deps: PasskeyDeps = browserDeps(),
): Promise<void> {
  const started = await deps.fetch(`${IDENTITY}/passkeys/challenge`, {
    method: 'POST',
    credentials: 'same-origin',
    signal: options.signal,
  });
  if (!started.ok) throw await problemFromResponse(started);
  const challenge = (await started.json()) as Options;
  const credential = await deps.credentials.get({
    publicKey: requestOptions(challenge.public_key),
    ...(options.mediation ? { mediation: options.mediation } : {}),
    ...(options.signal ? { signal: options.signal } : {}),
  });
  if (!credential) throw new DOMException('no passkey chosen', 'NotAllowedError');
  await session.signIn(`${IDENTITY}/passkeys/session`, {
    ceremony_id: challenge.ceremony_id,
    credential: credentialJson(credential),
  });
}

/** The API calls of a registration (typed clients in the app, fakes in tests). */
export interface RegistrationApi {
  options(password: string): Promise<Options>;
  finish(body: {
    ceremony_id: string;
    credential: Record<string, unknown>;
    name: string | null;
  }): Promise<unknown>;
}

/** Add a passkey to the signed-in user: password check, authenticator, verification. */
export async function registerPasskey(
  api: RegistrationApi,
  password: string,
  name: string | null,
  credentials: Pick<CredentialsContainer, 'create'> = navigator.credentials,
): Promise<void> {
  const started = await api.options(password);
  const credential = await credentials.create({ publicKey: creationOptions(started.public_key) });
  if (!credential) throw new DOMException('no passkey created', 'NotAllowedError');
  await api.finish({
    ceremony_id: started.ceremony_id,
    credential: credentialJson(credential),
    name,
  });
}

/** A default name for a new passkey: the device the browser runs on. */
export function deviceName(userAgent: string): string {
  if (/iPhone/.test(userAgent)) return 'iPhone';
  if (/iPad/.test(userAgent)) return 'iPad';
  if (/Android/.test(userAgent)) return 'Android';
  if (/Macintosh|Mac OS X/.test(userAgent)) return 'Mac';
  if (/Windows/.test(userAgent)) return 'Windows';
  if (/CrOS/.test(userAgent)) return 'Chromebook';
  if (/Linux/.test(userAgent)) return 'Linux';
  return 'Passkey';
}

/** A short device hint from the user agent of the registration: "Safari · iPhone". */
export function deviceHint(userAgent: string | null | undefined): string | null {
  if (!userAgent) return null;
  const browser = /Edg\//.test(userAgent)
    ? 'Edge'
    : /Firefox\//.test(userAgent)
      ? 'Firefox'
      : /Chrome\/|CriOS\//.test(userAgent)
        ? 'Chrome'
        : /Safari\//.test(userAgent)
          ? 'Safari'
          : null;
  const device = deviceName(userAgent);
  const parts = [browser, device === 'Passkey' ? null : device].filter(Boolean);
  return parts.length ? parts.join(' · ') : null;
}

/** Well-known passkey providers by AAGUID (from the community list passkey-authenticator-aaguids). */
const PROVIDERS: Record<string, string> = {
  'fbfc3007-154e-4ecc-8c0b-6e020557d7bd': 'iCloud Keychain',
  'dd4ec289-e01d-41c9-bb89-70fa845d4bf2': 'iCloud Keychain',
  'ea9b8d66-4d01-1d21-3ce4-b6b48cb575d4': 'Google Password Manager',
  'adce0002-35bc-c60a-648b-0b25f1f05503': 'Chrome on Mac',
  '08987058-cadc-4b81-b6e1-30de50dcbe96': 'Windows Hello',
  '9ddd1817-af5a-4672-a2b9-3e3dd95000a9': 'Windows Hello',
  '6028b017-b1d4-4c02-b4b3-afcdafc96bb2': 'Windows Hello',
  'bada5566-a7aa-401f-bd96-45619a55120d': '1Password',
  'd548826e-79b4-db40-a3d8-11116f7e8349': 'Bitwarden',
  '53414d53-554e-4700-0000-000000000000': 'Samsung Pass',
};

/** The passkey provider of an AAGUID, if it is a well-known one. */
export function providerName(aaguid: string | null | undefined): string | null {
  return aaguid ? (PROVIDERS[aaguid.toLowerCase()] ?? null) : null;
}

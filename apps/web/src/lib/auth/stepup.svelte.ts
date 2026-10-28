/**
 * Step-up re-authentication (ADR 0053).
 *
 * Critical actions (deleting a mower, creating an API token, changing roles, ...) answer `401
 * urn:mowdeck:problem:reauth-required` when the user has not confirmed with a passkey or the
 * password recently. The API client then calls {@link StepUp.request}: the re-authentication sheet
 * opens, the user confirms, Identity returns a new access token with a fresh `auth_time`, the
 * session adopts it and the client repeats the request. One sheet serves concurrent requests.
 *
 * Passkeys cannot run inside the native app's web view (ADR 0050); there only the password is
 * offered. The confirmation requests bypass the retrying API client on purpose: a wrong password
 * is a 401 as well and must not end the session.
 */
import { problemFromResponse, type ApiProblem } from '#lib/api/problem.js';
import { nativeBridge } from '#lib/native/capacitor.js';

import { passkeysEnabled } from './passkey';
import { IDENTITY, session, type SessionManager } from './session.svelte';
import { cancelled, credentialJson, requestOptions, webauthnSupported } from './webauthn';

export const REAUTH_TYPE = 'urn:mowdeck:problem:reauth-required';

/** True when `response` is the step-up challenge (a 401 whose problem type is reauth-required). */
export async function isReauthRequired(response: Response): Promise<boolean> {
  if (response.status !== 401) return false;
  try {
    const body = (await response.clone().json()) as { type?: unknown };
    return body.type === REAUTH_TYPE;
  } catch {
    return false;
  }
}

interface TokenBody {
  access_token: string;
  expires_in: number;
}

export interface StepUpDeps {
  fetch: typeof fetch;
  credentials: Pick<CredentialsContainer, 'get'> | null;
  /** The passkey button is offered: browser, not the app web view. */
  passkeyAllowed: () => boolean;
  passkeysEnabled: () => Promise<boolean>;
}

function browserDeps(): StepUpDeps {
  return {
    fetch: (...args) => globalThis.fetch(...args),
    credentials: typeof navigator === 'undefined' ? null : navigator.credentials,
    // Not inside the app web view: it has no access to passkeys (ADR 0050); the password does it.
    passkeyAllowed: () =>
      typeof window !== 'undefined' && nativeBridge(window) === null && webauthnSupported(window),
    passkeysEnabled: () => passkeysEnabled(),
  };
}

export class StepUp {
  /** The sheet is showing. */
  open = $state(false);
  /** A confirmation is running. */
  busy = $state(false);
  /** The last failed attempt, for the sheet. */
  problem = $state<ApiProblem | null>(null);
  /** The user dismissed the passkey dialog. */
  cancelledPasskey = $state(false);
  /** The passkey button is offered. */
  passkey = $state(false);

  #session: Pick<SessionManager, 'token' | 'adopt'>;
  #deps: StepUpDeps;
  #pending: Promise<string | null> | null = null;
  #settle: ((token: string | null) => void) | null = null;

  constructor(session: Pick<SessionManager, 'token' | 'adopt'>, deps: StepUpDeps = browserDeps()) {
    this.#session = session;
    this.#deps = deps;
  }

  /**
   * Ask the user to confirm. Resolves with the new access token, or null when they dismissed the
   * sheet. Concurrent callers share one sheet.
   */
  request(): Promise<string | null> {
    if (!this.#pending) {
      this.problem = null;
      this.cancelledPasskey = false;
      this.passkey = false;
      this.open = true;
      void this.#deps.passkeysEnabled().then((on) => {
        this.passkey = on && this.#deps.passkeyAllowed() && this.#deps.credentials !== null;
      });
      this.#pending = new Promise<string | null>((resolve) => {
        this.#settle = resolve;
      });
    }
    return this.#pending;
  }

  /** The user closed the sheet. */
  cancel(): void {
    this.#finish(null);
  }

  /** Confirm with the current password. */
  async withPassword(password: string): Promise<void> {
    await this.#run(async () => {
      const response = await this.#post('/me/step-up/password', { password });
      if (!response.ok) throw await problemFromResponse(response);
      return (await response.json()) as TokenBody;
    });
  }

  /** Confirm with one of the user's passkeys (user verification is required by the server). */
  async withPasskey(): Promise<void> {
    const credentials = this.#deps.credentials;
    if (!credentials) return;
    this.cancelledPasskey = false;
    await this.#run(async () => {
      const started = await this.#post('/me/step-up/passkey/options');
      if (!started.ok) throw await problemFromResponse(started);
      const options = (await started.json()) as {
        ceremony_id: string;
        public_key: Record<string, unknown>;
      };
      let credential: Credential | null;
      try {
        credential = await credentials.get({ publicKey: requestOptions(options.public_key) });
      } catch (error) {
        if (cancelled(error)) {
          this.cancelledPasskey = true;
          return null;
        }
        throw error;
      }
      if (!credential) {
        this.cancelledPasskey = true;
        return null;
      }
      const response = await this.#post('/me/step-up/passkey', {
        ceremony_id: options.ceremony_id,
        credential: credentialJson(credential),
      });
      if (!response.ok) throw await problemFromResponse(response);
      return (await response.json()) as TokenBody;
    });
  }

  async #run(task: () => Promise<TokenBody | null>): Promise<void> {
    this.busy = true;
    this.problem = null;
    try {
      const body = await task();
      if (body) {
        this.#session.adopt(body.access_token, body.expires_in);
        this.#finish(body.access_token);
      }
    } catch (error) {
      this.problem =
        error && typeof error === 'object' && 'status' in error ? (error as ApiProblem) : null;
      if (!this.problem) throw error;
    } finally {
      this.busy = false;
    }
  }

  #finish(token: string | null): void {
    const settle = this.#settle;
    this.open = false;
    this.#pending = null;
    this.#settle = null;
    settle?.(token);
  }

  #post(path: string, body?: object): Promise<Response> {
    const token = this.#session.token;
    return this.#deps.fetch(`${IDENTITY}${path}`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
  }
}

/** The step-up confirmation of this tab. */
export const stepUp = new StepUp(session);

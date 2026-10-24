/**
 * Is the signed-in user an operator admin of the installation (`platform:main#operator_admin`)?
 *
 * Neither the token nor `GET /identity/me` says so (the relation lives only in OpenFGA), so the
 * shell asks once per session: `GET /identity/me/platform` answers `{"operator_admin": true}`
 * for operator admins. Anything else (false, a 404 from an Identity without the endpoint, a
 * network failure) hides the operator console quietly; it is no error, so nothing is logged.
 * The services check every operator request again.
 */
import { apiFetch } from '#lib/api/client.js';

export type ProbeFn = () => Promise<boolean>;

export const PLATFORM_PROBE = '/api/v1/identity/me/platform';

/** Ask `fetcher` whether the user is an operator admin; any failure means no. */
export async function probeOperator(fetcher: typeof fetch): Promise<boolean> {
  try {
    const response = await fetcher(PLATFORM_PROBE, { headers: { Accept: 'application/json' } });
    if (!response.ok) return false;
    const body = (await response.json()) as { operator_admin?: unknown } | null;
    return body?.operator_admin === true;
  } catch {
    return false;
  }
}

const defaultProbe: ProbeFn = () => probeOperator(apiFetch);

export class OperatorAccess {
  /** True once the probe answered 200. */
  allowed = $state(false);
  /** True once the probe finished (either way). */
  checked = $state(false);
  #pending: Promise<boolean> | null = null;
  #probe: ProbeFn;

  constructor(probe: ProbeFn = defaultProbe) {
    this.#probe = probe;
  }

  /** Probe once; later calls share the first answer until {@link reset}. */
  load(): Promise<boolean> {
    this.#pending ??= this.#probe().then((allowed) => {
      this.allowed = allowed;
      this.checked = true;
      return allowed;
    });
    return this.#pending;
  }

  /** Forget the answer (logout, another user). */
  reset(): void {
    this.#pending = null;
    this.allowed = false;
    this.checked = false;
  }
}

export const operatorAccess = new OperatorAccess();

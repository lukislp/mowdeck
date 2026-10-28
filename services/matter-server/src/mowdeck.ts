/**
 * MowDeck's API, two ways:
 *
 * - {@link MowDeckClient}: the server's own access with an API token (one per account). The API
 *   token is exchanged for a 10 minute access token (`POST /identity/token`), renewed a minute
 *   before it expires and once after a 401. It needs `mower.view` and `mower.control` and
 *   nothing else (ADR 0063).
 * - {@link checkUserPermissions}: the caller of the admin API (the web app) is asked about with
 *   the caller's own access token, so Identity verifies the token and OpenFGA answers per mower.
 */
export class MowDeckError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'MowDeckError';
    this.status = status;
  }
}

export interface MowerInfo {
  id: string;
  name: string;
  model: string | null;
  serial: string | null;
}

type Fetch = typeof fetch;

const TIMEOUT_MS = 10_000;

async function call(
  fetcher: Fetch,
  url: string,
  init: RequestInit & { token?: string } = {},
): Promise<unknown> {
  const headers = new Headers(init.headers);
  headers.set('Accept', 'application/json');
  if (init.token) headers.set('Authorization', `Bearer ${init.token}`);
  if (init.body !== undefined) headers.set('Content-Type', 'application/json');
  const response = await fetcher(url, {
    ...init,
    headers,
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  if (response.status === 204) return null;
  const text = await response.text();
  if (!response.ok) {
    throw new MowDeckError(response.status, `${init.method ?? 'GET'} ${new URL(url).pathname}`);
  }
  return text ? (JSON.parse(text) as unknown) : null;
}

export class MowDeckClient {
  readonly #base: string;
  readonly #apiToken: string;
  readonly #fetch: Fetch;
  readonly #now: () => number;
  #access: string | null = null;
  #expiresAt = 0;
  #pending: Promise<string> | null = null;
  /** The account the token belongs to (from `GET /identity/me`). */
  accountId: string | null = null;

  constructor(base: string, apiToken: string, fetcher: Fetch = fetch, now = Date.now) {
    this.#base = base;
    this.#apiToken = apiToken;
    this.#fetch = fetcher;
    this.#now = now;
  }

  async #token(force = false): Promise<string> {
    if (!force && this.#access && this.#now() < this.#expiresAt - 60_000) return this.#access;
    this.#pending ??= this.#exchange().finally(() => {
      this.#pending = null;
    });
    return this.#pending;
  }

  async #exchange(): Promise<string> {
    const body = (await call(this.#fetch, `${this.#base}/api/v1/identity/token`, {
      method: 'POST',
      body: JSON.stringify({ grant_type: 'api_token', token: this.#apiToken }),
    })) as { access_token?: unknown; expires_in?: unknown } | null;
    if (!body || typeof body.access_token !== 'string') {
      throw new MowDeckError(502, 'token exchange returned no access token');
    }
    const seconds = typeof body.expires_in === 'number' ? body.expires_in : 600;
    this.#access = body.access_token;
    this.#expiresAt = this.#now() + seconds * 1000;
    return this.#access;
  }

  async #request(path: string, init: RequestInit = {}): Promise<unknown> {
    try {
      return await call(this.#fetch, `${this.#base}${path}`, {
        ...init,
        token: await this.#token(),
      });
    } catch (error) {
      if (!(error instanceof MowDeckError) || error.status !== 401) throw error;
      // Revoked keys or a rotation: exchange once more; a revoked API token fails here again.
      return call(this.#fetch, `${this.#base}${path}`, { ...init, token: await this.#token(true) });
    }
  }

  async me(): Promise<string> {
    const body = (await this.#request('/api/v1/identity/me')) as {
      account_id?: unknown;
      acc?: unknown;
    } | null;
    const account = body?.account_id ?? body?.acc;
    this.accountId = typeof account === 'string' ? account : null;
    return this.accountId ?? '';
  }

  /** `GET /fleet/mowers`: the mowers the token sees. */
  async mowers(): Promise<MowerInfo[]> {
    const body = (await this.#request('/api/v1/fleet/mowers')) as { items?: unknown } | null;
    const items = Array.isArray(body?.items) ? body.items : [];
    return items.flatMap((item): MowerInfo[] => {
      if (typeof item !== 'object' || item === null) return [];
      const m = item as Record<string, unknown>;
      if (typeof m.id !== 'string' || typeof m.name !== 'string') return [];
      return [
        {
          id: m.id,
          name: m.name,
          model: typeof m.model === 'string' ? m.model : null,
          serial: typeof m.serial === 'string' ? m.serial : null,
        },
      ];
    });
  }

  /** `GET /telemetry/mowers/{id}/widget`: the compact state; null before the first state. */
  async widget(mowerId: string): Promise<unknown> {
    try {
      return await this.#request(`/api/v1/telemetry/mowers/${encodeURIComponent(mowerId)}/widget`);
    } catch (error) {
      if (error instanceof MowDeckError && error.status === 404) return null;
      throw error;
    }
  }

  /** The ids of every area of the mower's map, in map order. */
  async areaIds(mowerId: string): Promise<string[]> {
    const body = (await this.#request(
      `/api/v1/telemetry/mowers/${encodeURIComponent(mowerId)}/map`,
    )) as { map?: { areas?: unknown } } | null;
    const areas = Array.isArray(body?.map?.areas) ? body.map.areas : [];
    return areas.flatMap((area) =>
      typeof area === 'object' && area !== null && typeof (area as { id?: unknown }).id === 'string'
        ? [(area as { id: string }).id]
        : [],
    );
  }

  /** The saved mowing pattern (`mow_defaults.pattern`), or null. */
  async mowPattern(mowerId: string): Promise<Record<string, unknown> | null> {
    const body = (await this.#request(`/api/v1/fleet/mowers/${encodeURIComponent(mowerId)}`)) as {
      mow_defaults?: { pattern?: unknown } | null;
    } | null;
    const pattern = body?.mow_defaults?.pattern;
    return typeof pattern === 'object' && pattern !== null
      ? (pattern as Record<string, unknown>)
      : null;
  }

  /** `POST /fleet/mowers/{id}/commands`; Fleet checks the permission per command kind. */
  async command(mowerId: string, command: Record<string, unknown>): Promise<void> {
    await this.#request(`/api/v1/fleet/mowers/${encodeURIComponent(mowerId)}/commands`, {
      method: 'POST',
      body: JSON.stringify({ command }),
    });
  }
}

export interface PermissionAnswer {
  view: boolean;
  control: boolean;
}

/**
 * May the holder of `authorization` (the web app's `Authorization` header) view and control
 * each mower? Throws {@link MowDeckError} 401 for a missing or rejected token.
 */
export async function checkUserPermissions(
  base: string,
  authorization: string | undefined,
  mowerIds: readonly string[],
  fetcher: Fetch = fetch,
): Promise<Map<string, PermissionAnswer>> {
  const match = /^Bearer\s+(\S+)$/i.exec(authorization ?? '');
  if (!match?.[1]) throw new MowDeckError(401, 'missing bearer token');
  const result = new Map<string, PermissionAnswer>();
  const ids = [...new Set(mowerIds)];
  // Identity answers at most 100 checks per request: 50 mowers with two relations each.
  for (let start = 0; start < ids.length; start += 50) {
    const batch = ids.slice(start, start + 50);
    const checks = batch.flatMap((id) => [
      { relation: 'can_view', object: `mower:${id}` },
      { relation: 'can_control', object: `mower:${id}` },
    ]);
    const answers = await call(fetcher, `${base}/api/v1/identity/me/permissions`, {
      method: 'POST',
      token: match[1],
      body: JSON.stringify({ checks }),
    });
    if (!Array.isArray(answers) || answers.length !== checks.length) {
      throw new MowDeckError(502, 'permission check returned an unexpected answer');
    }
    batch.forEach((id, index) => {
      result.set(id, {
        view: answers[index * 2] === true,
        control: answers[index * 2 + 1] === true,
      });
    });
  }
  return result;
}

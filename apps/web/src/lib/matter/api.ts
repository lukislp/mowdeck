/**
 * Matter server (`/api/v1/matter`, ADR 0063): the mowers that are Matter nodes and their pairing
 * codes. A TypeScript service without an exported OpenAPI document, so the few shapes are typed
 * here. Codes are secrets: they live only in the component that shows them, never in a store,
 * a URL or a log.
 */
import { apiFetch } from '#lib/api/client.js';
import { problemFromResponse } from '#lib/api/problem.js';

export interface MatterMower {
  mower_id: string;
  name: string;
  commissioned: boolean;
  /** Controllers (Apple Home, Google Home, Home Assistant, ...) that paired the mower. */
  fabric_count: number;
}

export interface MatterPairing extends MatterMower {
  /** `1234-567-8901`, as Apple Home shows it. */
  manual_code: string;
  /** The QR payload (`MT:...`). */
  qr_payload: string;
  /** Rows of the QR code, `1` for a dark module, without the quiet zone. */
  qr_matrix: string[];
}

export interface MatterApi {
  mowers(): Promise<MatterMower[]>;
  pairing(mowerId: string): Promise<MatterPairing>;
  reset(mowerId: string): Promise<MatterPairing>;
}

const BASE = '/api/v1/matter';

export function matterCalls(fetcher: typeof fetch = apiFetch): MatterApi {
  async function call<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetcher(`${BASE}${path}`, { cache: 'no-store', ...init });
    if (!response.ok) throw await problemFromResponse(response);
    return (await response.json()) as T;
  }
  return {
    mowers: async () => (await call<{ items: MatterMower[] }>('/mowers')).items,
    pairing: (id) => call<MatterPairing>(`/mowers/${encodeURIComponent(id)}/pairing`),
    reset: (id) =>
      call<MatterPairing>(`/mowers/${encodeURIComponent(id)}/pairing/reset`, { method: 'POST' }),
  };
}

/** The dark modules of a QR matrix as one SVG path (`M x y h1v1h-1z` per module), drawn with
 * a quiet zone of `margin` modules. */
export function qrPath(rows: readonly string[], margin = 4): { d: string; size: number } {
  const parts: string[] = [];
  rows.forEach((row, y) => {
    for (let x = 0; x < row.length; x++) {
      if (row[x] === '1') parts.push(`M${x + margin} ${y + margin}h1v1h-1z`);
    }
  });
  return { d: parts.join(''), size: (rows[0]?.length ?? 0) + margin * 2 };
}

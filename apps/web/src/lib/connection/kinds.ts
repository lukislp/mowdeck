/**
 * The connection kinds this installation accepts, with their option schemas
 * (`GET /api/v1/fleet/connection-kinds`), loaded once per page load.
 */
import { fleetApi, unwrap } from '#lib/api/client.js';
import { getLocale } from '#lib/paraglide/runtime.js';

import type { KindSchema } from './form';

let loading: Promise<KindSchema[]> | null = null;

export function loadKinds(): Promise<KindSchema[]> {
  // Provider options come with their texts in the UI language (ADR 0066).
  loading ??= unwrap(
    fleetApi.GET('/api/v1/fleet/connection-kinds', { params: { query: { locale: getLocale() } } }),
  )
    .then((list) => list.items)
    .catch((error: unknown) => {
      loading = null;
      throw error;
    });
  return loading;
}

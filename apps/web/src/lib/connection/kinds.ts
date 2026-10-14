/**
 * The connection kinds this installation accepts, with their option schemas
 * (`GET /api/v1/fleet/connection-kinds`), loaded once per page load.
 */
import { fleetApi, unwrap } from '$lib/api/client';

import type { KindSchema } from './form';

let loading: Promise<KindSchema[]> | null = null;

export function loadKinds(): Promise<KindSchema[]> {
  loading ??= unwrap(fleetApi.GET('/api/v1/fleet/connection-kinds'))
    .then((list) => list.items)
    .catch((error: unknown) => {
      loading = null;
      throw error;
    });
  return loading;
}

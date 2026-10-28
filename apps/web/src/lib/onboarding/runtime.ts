/**
 * The setup tour wired to the services: the choices at Identity, the facts from Fleet,
 * Telemetry, Notifications, Scheduler and Identity, the context from the session, the module
 * registry and the app shell. Every probe is guarded on its own: one failing service leaves
 * that one fact unknown (the step stays open), never the whole tour.
 */
import {
  fleetApi,
  identityApi,
  notificationsApi,
  schedulerApi,
  telemetryApi,
  unwrap,
} from '#lib/api/client.js';
import { session } from '#lib/auth/session.svelte.js';
import { loadKinds } from '#lib/connection/kinds.js';
import { nativeShell } from '#lib/native/runtime.js';
import { operatorAccess } from '#lib/operator/access.svelte.js';
import { m } from '#lib/paraglide/messages.js';
import { builtIn, registry, visibleModules } from '#lib/shell/registry.svelte.js';
import { toasts } from '#lib/stores/ui.svelte.js';

import { Onboarding } from './onboarding.svelte';
import type { Context, Facts, Progress, TourStatus } from './steps';

/** At most this many mowers are asked for a map. */
const MAP_PROBES = 3;

async function count(call: () => Promise<{ length: number } | { items: unknown[] }>) {
  try {
    const found = await call();
    return 'items' in found ? found.items.length : found.length;
  } catch {
    return null;
  }
}

/** A request that answers 200 (true), any other status (false) or fails (null). */
async function ok(call: () => Promise<{ response: Response }>): Promise<boolean | null> {
  try {
    const { response } = await call();
    return response.ok;
  } catch {
    return null;
  }
}

export async function readFacts(): Promise<Facts> {
  let mowerIds: string[] | null;
  try {
    const list = await unwrap(fleetApi.GET('/api/v1/fleet/mowers'));
    mowerIds = list.items.map((mower) => mower.id);
  } catch {
    mowerIds = null;
  }
  const canManage = session.has('fleet.manage');
  const [cloudAccounts, maps, push, apns, ntfy, passkeys, schedules, signInLinks] =
    await Promise.all([
      canManage
        ? count(() => unwrap(fleetApi.GET('/api/v1/fleet/linked-accounts')))
        : Promise.resolve(null),
      Promise.all(
        (mowerIds ?? []).slice(0, MAP_PROBES).map((id) =>
          ok(() =>
            telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/map', {
              params: { path: { mower_id: id } },
            }),
          ),
        ),
      ),
      count(() => unwrap(notificationsApi.GET('/api/v1/notifications/push-subscriptions'))),
      count(() => unwrap(notificationsApi.GET('/api/v1/notifications/me/push/apns'))),
      ok(() => notificationsApi.GET('/api/v1/notifications/ntfy')),
      count(() => unwrap(identityApi.GET('/api/v1/identity/me/passkeys'))),
      count(() => unwrap(schedulerApi.GET('/api/v1/scheduler/schedules'))),
      count(() => unwrap(identityApi.GET('/api/v1/identity/me/external-identities'))),
    ]);
  const channels = [push !== null ? push > 0 : null, apns !== null ? apns > 0 : null, ntfy];
  return {
    mowers: mowerIds?.length ?? null,
    firstMowerId: mowerIds?.[0] ?? null,
    cloudAccounts,
    map: maps.includes(true) ? true : mowerIds === null || maps.includes(null) ? null : false,
    notifications: channels.includes(true) ? true : channels.includes(null) ? null : false,
    passkeys,
    schedules,
    signInLinks,
    appExtras: nativeShell.widgetsAvailable ? nativeShell.widgets === 'on' : null,
  };
}

let methods: Promise<{ passkey: boolean; github: boolean; apple: boolean }> | null = null;

function loginMethods() {
  methods ??= unwrap(identityApi.GET('/api/v1/identity/login-methods')).catch(() => {
    methods = null;
    return { passkey: false, github: false, apple: false };
  });
  return methods;
}

export async function readContext(): Promise<Context> {
  const visible = new Set(
    visibleModules(
      builtIn,
      session.me?.permissions ?? [],
      registry.areas,
      operatorAccess.allowed,
    ).map((module) => module.id),
  );
  const canManage = session.has('fleet.manage');
  const [offered, cloud] = await Promise.all([
    loginMethods(),
    canManage
      ? loadKinds()
          .then((kinds) => kinds.some((kind) => kind.linked_account === true))
          .catch(() => false)
      : Promise.resolve(false),
  ]);
  return {
    canManage,
    cloud,
    schedules: visible.has('schedules'),
    notifications: visible.has('notification-settings'),
    passkeys: offered.passkey,
    signInLinks: offered.github || offered.apple,
    appExtras: nativeShell.widgetsAvailable,
  };
}

const ONBOARDING = '/api/v1/identity/me/onboarding';

function progressOf(body: {
  status: TourStatus;
  skipped_steps: string[];
  dismissed_hints: string[];
}): Progress {
  return { status: body.status, skipped: body.skipped_steps, hints: body.dismissed_hints };
}

export const onboarding = new Onboarding({
  api: {
    load: async () => progressOf(await unwrap(identityApi.GET(ONBOARDING))),
    save: async (progress) =>
      progressOf(
        await unwrap(
          identityApi.PUT(ONBOARDING, {
            body: {
              status: progress.status === 'new' ? 'dismissed' : progress.status,
              skipped_steps: progress.skipped,
              dismissed_hints: progress.hints,
            },
          }),
        ),
      ),
  },
  facts: readFacts,
  context: readContext,
  onSaveFailed: () => toasts.push('error', m.onboarding_save_failed()),
});

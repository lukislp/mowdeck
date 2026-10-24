/**
 * Wires the session, the Realtime socket and the stores together for this tab: states go to the
 * mower store, command results to the command store (and a toast), revocations remove mowers,
 * a refreshed token re-authenticates the socket, logout closes it.
 */
import { SvelteSet } from 'svelte/reactivity';

import { fleetApi, identityApi, unwrap } from '$lib/api/client';
import { asProblem } from '$lib/api/problem';
import { session } from '$lib/auth/session.svelte';
import { commandLabel, outcomeLabel, problemMessage } from '$lib/i18n';
import { quietSettingResult } from '$lib/mower/device-settings';
import { quietLightResult } from '$lib/mower/light';
import { m } from '$lib/paraglide/messages';
import { RealtimeClient } from '$lib/realtime/client.svelte';
import { CommandStore, type CommandKind } from '$lib/stores/commands.svelte';
import { lastLoad } from '$lib/stores/last-load';
import { mowers } from '$lib/stores/mowers.svelte';
import { toasts } from '$lib/stores/ui.svelte';

/** Mowers whose access ended while the app was open (the detail page says so). */
export const revoked = new SvelteSet<string>();

export const commands = new CommandStore({
  post: (mowerId, command) =>
    unwrap(
      fleetApi.POST('/api/v1/fleet/mowers/{mower_id}/commands', {
        params: { path: { mower_id: mowerId } },
        // The command union is validated by the server; the store sends known kinds only.
        body: { command: command as never, ttl_seconds: 30 },
      }),
    ),
  status: async (commandId) => {
    const view = await unwrap(
      fleetApi.GET('/api/v1/fleet/commands/{command_id}', {
        params: { path: { command_id: commandId } },
      }),
    );
    return { state: view.status, reason: view.reason ?? null };
  },
});

commands.onFinal((command) => {
  if (command.kind === 'emergency_stop' && command.status === 'executed') return;
  if (quietLightResult(command, realtime.drive?.mowerId ?? null)) return;
  if (quietSettingResult(command)) return;
  toasts.push(
    command.status === 'executed' ? 'ok' : 'error',
    m.command_toast({ command: commandLabel(command.kind), result: outcomeLabel(command.status) }),
    command.reason ?? undefined,
  );
});

function socketUrl(): string {
  const { protocol, host } = window.location;
  return `${protocol === 'https:' ? 'wss' : 'ws'}://${host}/api/v1/realtime/ws`;
}

export const realtime = new RealtimeClient({
  url: socketUrl,
  getToken: (force, rejected) => session.validToken(force, rejected).catch(() => null),
  onState: (message) => {
    mowers.upsert(String(message.mower_id), message.state as never, 'live');
  },
  onResult: (message) => {
    const result = message.result;
    commands.resolve(String(result.command_id), result.status, result.reason ?? null);
  },
  onRevoked: (ids) => {
    for (const id of ids) {
      revoked.add(id);
      mowers.remove(id);
    }
    permissions.clear();
  },
  onDenied: (ids) => {
    for (const id of ids) revoked.add(id);
  },
  onSessionLost: () => session.expire(),
});

// --- per mower permissions (POST /me/permissions), cached 60 s ---------------------------------

export type MowerRelation =
  | 'can_control'
  | 'can_drive'
  | 'can_manage_mowers'
  | 'can_emergency_stop'
  | 'can_edit_schedule'
  | 'can_bulk_command';
/** An object of the authorization model the UI asks about (`mower:<id>`, `group:<id>`, ...). */
export type ObjectRef = `${'account' | 'site' | 'group' | 'mower'}:${string}`;

class Permissions {
  #cache = new Map<string, { at: number; value: boolean }>();
  version = $state(0);

  /** The relations on one mower (cached 60 s). */
  async check(
    mowerId: string,
    relations: MowerRelation[],
  ): Promise<Record<MowerRelation, boolean>> {
    const answers = await this.checkObjects([`mower:${mowerId}`], relations);
    return answers[`mower:${mowerId}`] ?? ({} as Record<MowerRelation, boolean>);
  }

  /** The relations on several objects in one request (at most 100 questions; cached 60 s). */
  async checkObjects(
    objects: ObjectRef[],
    relations: MowerRelation[],
  ): Promise<Record<string, Record<MowerRelation, boolean>>> {
    const now = Date.now();
    const missing: { relation: MowerRelation; object: ObjectRef }[] = [];
    for (const object of new Set(objects)) {
      for (const relation of relations) {
        const hit = this.#cache.get(`${relation}|${object}`);
        if (!hit || now - hit.at > 60_000) missing.push({ relation, object });
      }
    }
    for (let i = 0; i < missing.length; i += 100) {
      const chunk = missing.slice(i, i + 100);
      const answers = await unwrap(
        identityApi.POST('/api/v1/identity/me/permissions', { body: { checks: chunk } }),
      );
      chunk.forEach(({ relation, object }, j) => {
        this.#cache.set(`${relation}|${object}`, { at: now, value: answers[j] === true });
      });
    }
    const result: Record<string, Record<MowerRelation, boolean>> = {};
    for (const object of objects) {
      const entry = {} as Record<MowerRelation, boolean>;
      for (const relation of relations) {
        entry[relation] = this.#cache.get(`${relation}|${object}`)?.value ?? false;
      }
      result[object] = entry;
    }
    return result;
  }

  clear(): void {
    this.#cache.clear();
    this.version += 1;
  }
}

export const permissions = new Permissions();

// --- lifecycle ------------------------------------------------------------------------------------

let started = false;

/** Called once by the root layout in the browser. */
export function startLive(): void {
  if (started || typeof window === 'undefined') return;
  started = true;
  session.onToken((token) => {
    if (token) {
      realtime.reauth(token);
    } else {
      realtime.stop();
      mowers.clear();
      commands.clear();
      permissions.clear();
      revoked.clear();
      lastLoad.clear();
    }
  });
  window.addEventListener('online', () => realtime.wake());
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') realtime.wake();
  });
}

/** Send a command and report a refusal of the API as a toast; returns whether it was accepted. */
export async function sendCommand(
  mowerId: string,
  command: { kind: CommandKind } & Record<string, unknown>,
): Promise<boolean> {
  try {
    await commands.send(mowerId, command);
    return true;
  } catch (error) {
    const problem = asProblem(error);
    toasts.push(
      'error',
      m.command_toast({ command: commandLabel(command.kind), result: problemMessage(problem) }),
      problem.detail ?? undefined,
      problem.correlationId ?? undefined,
    );
    return false;
  }
}

/**
 * Chat control (the optional assistant add-on, ADR 0075): the calls, the gate and the
 * execution of a confirmed proposal.
 *
 * The assistant service only proposes. After the user confirmed the card, this module sends
 * every action through the regular endpoints with the user's own token, exactly like the
 * buttons of the mower page: Fleet's `POST /mowers/{id}/commands` for the immediate actions and
 * the Scheduler's `POST /schedules` (kind `once`) for a later start. So authorization (ADR
 * 0016), plans, the controller's safety rules (ADR 0012) and the audit apply unchanged. Pure
 * apart from the injected calls, unit tested in `assistant.test.ts`.
 */
import { assistantApi, unwrap } from '#lib/api/client.js';
import type { components } from '#lib/generated/assistant.js';
import { m } from '#lib/paraglide/messages.js';
import type { Locale, ScheduleIn } from '#lib/schedules/types.js';
import type { CommandKind } from '#lib/stores/commands.svelte.js';

type Schemas = components['schemas'];

export type AssistantStatus = Schemas['StatusOut'];
export type Proposal = Schemas['ProposalOut'];
export type ProposedAction = Schemas['ActionOut'];
export type ActionKind = Schemas['ActionKind'];
export type Note = Schemas['Note'];
export type Rejection = Schemas['Rejection'];
export type Unavailable = Schemas['Unavailable'];

/** The UI area the assistant announces (`module.py`): the chat box exists only with it. */
export const ASSISTANT_AREA = '/assistant';

/** Request length limit of the service. */
export const TEXT_MAX = 500;

/** Example requests offered as chips (also what the phrase parser of the demo understands). */
export function examples(): string[] {
  return [m.chat_example_later(), m.chat_example_dock(), m.chat_example_pause()];
}

/** Whether the add-on is deployed: its UI area is announced. Unknown areas (the endpoint
 * failed) hide it, unlike the menu: a chat box that cannot answer helps nobody. */
export function assistantEnabled(areas: readonly { route: string }[] | null): boolean {
  return areas?.some((area) => area.route === ASSISTANT_AREA) ?? false;
}

export async function loadStatus(): Promise<AssistantStatus> {
  return unwrap(assistantApi.GET('/api/v1/assistant/status'));
}

export async function propose(mowerId: string, text: string, timezone: string): Promise<Proposal> {
  return unwrap(
    assistantApi.POST('/api/v1/assistant/mowers/{mower_id}/proposals', {
      params: { path: { mower_id: mowerId } },
      body: { text, timezone },
    }),
  );
}

/** The browser's IANA time zone (the assistant schedules a later start in it). */
export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

export type MowerCommand =
  | { kind: 'start_mow'; area_ids: string[] }
  | { kind: 'pause_mow' }
  | { kind: 'resume_mow' }
  | { kind: 'return_to_dock' };

/** The Fleet command of an immediate action; null for a later start. */
export function commandOf(action: ProposedAction): (MowerCommand & { kind: CommandKind }) | null {
  switch (action.kind) {
    case 'start_now':
      return { kind: 'start_mow', area_ids: [...action.area_ids] };
    case 'pause':
      return { kind: 'pause_mow' };
    case 'resume':
      return { kind: 'resume_mow' };
    case 'dock':
      return { kind: 'return_to_dock' };
    case 'start_later':
      return null;
  }
}

/** Names of the action's areas ("all areas" when it mows every area of the mower). */
export function areaSummary(action: ProposedAction, proposal: Proposal): string {
  if (!action.area_ids.length) return '';
  if (action.area_ids.length === proposal.areas.length && proposal.areas.length > 1) {
    return m.chat_all_areas();
  }
  return action.areas.map((area) => area.name).join(', ');
}

/** `YYYY-MM-DDTHH:MM` as date and time words in the locale ("Mo., 2. Nov., 08:00"). */
export function formatLocal(local: string, locale: Locale): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(local);
  if (!match) return local;
  const [, y, mo, d, h, mi] = match.map(Number) as [number, number, number, number, number, number];
  const date = new Date(y, mo - 1, d, h, mi);
  return new Intl.DateTimeFormat(locale, {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

/** The body of the one-off schedule of a later start (Scheduler `kind: once`). */
export function scheduleOf(action: ProposedAction, mowerId: string, name: string): ScheduleIn {
  const [day, time] = (action.start_local ?? '').split('T');
  return {
    name: name.slice(0, 100),
    target_type: 'mower',
    target_id: mowerId,
    kind: 'once',
    days_of_week: [],
    anchor_date: day ?? null,
    start_time: time ?? '',
    duration_minutes: action.duration_minutes ?? 120,
    timezone: action.timezone ?? null,
    area_ids: [...action.area_ids],
    enabled: true,
    pause_running_jobs_on_rain: true,
    return_to_dock_at_window_end: false,
  };
}

/** The schedule's name: what it does, so the schedules list shows it ("Chat · Back lawn"). */
export function scheduleName(action: ProposedAction, proposal: Proposal): string {
  return m.chat_schedule_name({ areas: areaSummary(action, proposal) || proposal.mower_name });
}

/** A proposal may be executed until `expires_at`; after that the user asks again. */
export function expired(proposal: Proposal, now: number): boolean {
  return Date.parse(proposal.expires_at) <= now;
}

export interface Executor {
  /** Send a Fleet command (toasts its own problems); false when it was refused. */
  command(mowerId: string, command: MowerCommand & { kind: CommandKind }): Promise<boolean>;
  /** Create a schedule; false when it was refused. */
  schedule(body: ScheduleIn): Promise<boolean>;
}

export interface ExecutionResult {
  /** Actions sent, in order. */
  done: number;
  /** The action that failed (the rest was not sent), or null. */
  failedAt: number | null;
}

/**
 * Execute a confirmed proposal: every action in order, stopping at the first refusal so a
 * refused dock never leaves a later start behind. Never runs an expired or not executable
 * proposal.
 */
export async function execute(
  proposal: Proposal,
  executor: Executor,
  now: number,
): Promise<ExecutionResult> {
  if (!proposal.executable || expired(proposal, now) || !proposal.actions.length) {
    return { done: 0, failedAt: 0 };
  }
  let done = 0;
  for (const action of proposal.actions) {
    const command = commandOf(action);
    const ok = command
      ? await executor.command(proposal.mower_id, command)
      : await executor.schedule(
          scheduleOf(action, proposal.mower_id, scheduleName(action, proposal)),
        );
    if (!ok) return { done, failedAt: done };
    done += 1;
  }
  return { done, failedAt: null };
}

export function actionLabel(kind: ActionKind): string {
  switch (kind) {
    case 'start_now':
      return m.chat_action_start_now();
    case 'start_later':
      return m.chat_action_start_later();
    case 'pause':
      return m.chat_action_pause();
    case 'resume':
      return m.chat_action_resume();
    case 'dock':
      return m.chat_action_dock();
  }
}

export function noteText(note: Note): string {
  switch (note) {
    case 'manual_drive':
      return m.chat_note_manual_drive();
    case 'settings':
      return m.chat_note_settings();
    case 'timed_resume':
      return m.chat_note_timed_resume();
    case 'other_mower':
      return m.chat_note_other_mower();
    case 'unsupported':
      return m.chat_note_unsupported();
    case 'unclear':
      return m.chat_note_unclear();
  }
}

export function rejectionText(code: Rejection): string {
  switch (code) {
    case 'not_offered':
      return m.chat_reject_not_offered();
    case 'unknown_area':
    case 'too_many_areas':
      return m.chat_reject_area();
    case 'time_missing':
    case 'time_invalid':
      return m.chat_reject_time();
    case 'time_past':
      return m.chat_reject_time_past();
    case 'time_too_far':
      return m.chat_reject_time_far();
    default:
      return m.chat_reject_other();
  }
}

/** Why nothing is offered at all (every kind unavailable for the same reason), or null. */
export function blockedText(proposal: Proposal): string | null {
  const reasons = new Set(Object.values(proposal.unavailable));
  if (Object.keys(proposal.unavailable).length < 5 || reasons.size !== 1) return null;
  const [reason] = [...reasons] as Unavailable[];
  switch (reason) {
    case 'permission':
      return m.chat_blocked_permission();
    case 'not_connected':
      return m.chat_blocked_not_connected();
    default:
      return null;
  }
}

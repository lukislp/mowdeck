/**
 * Connection forms generated from Fleet's option schemas (`GET /api/v1/fleet/connection-kinds`):
 * the form state for a kind, validation that mirrors Fleet's rules, the options to send, and
 * the server's 422 field errors mapped back to the inputs. Pure functions, no DOM.
 *
 * Secrets are write-only: their inputs start empty; when the server answers `***` the option
 * is "set", and an untouched set secret is sent back as `***`, which keeps the stored value.
 * Managed options (a linked account's `device_name` and `iot_id`) are set by Fleet: shown
 * read-only, never validated and never sent.
 */
import type { ApiProblem } from '$lib/api/problem';
import type { components } from '$lib/generated/fleet';

export type KindSchema = components['schemas']['ConnectionKindOut'];
export type OptionSchema = components['schemas']['ConnectionOptionOut'];

export const SECRET_MASK = '***';

export interface ConnectionState {
  /** Text inputs (strings and numbers) by option key. */
  values: Record<string, string>;
  /** Switches by option key. */
  flags: Record<string, boolean>;
  /** Secrets the server holds for this mower (answered as `***`). */
  secretSet: Record<string, boolean>;
  /** The chosen alternative per group (e.g. `credential` -> `credential_ref`). */
  choice: Record<string, string>;
}

export type IssueCode = 'required' | 'pattern' | 'range' | 'integer' | 'number';

export interface FieldIssue {
  code: IssueCode | 'server';
  /** The server's message for `server` issues of unknown codes. */
  message?: string;
}

export function groupMembers(schema: KindSchema, group: string): OptionSchema[] {
  return schema.options.filter((o) => o.group === group);
}

/** Set by Fleet (from a linked account's device list): read-only in the form, never sent. */
export function isManaged(option: OptionSchema): boolean {
  return option.managed === true;
}

/** The options to render: ungrouped ones and the chosen member of each group. */
export function isActive(option: OptionSchema, state: ConnectionState): boolean {
  return !option.group || state.choice[option.group] === option.key;
}

function filled(option: OptionSchema, state: ConnectionState): boolean {
  if (option.type === 'boolean') return true;
  if ((state.values[option.key] ?? '').trim() !== '') return true;
  return option.secret && state.secretSet[option.key] === true;
}

/**
 * The form state for `schema`. `stored` are the mower's (masked) options when the form edits a
 * mower of this kind; for a new mower or another kind pass null (nothing is carried over).
 */
export function initialState(
  schema: KindSchema,
  stored: Record<string, unknown> | null,
): ConnectionState {
  const state: ConnectionState = { values: {}, flags: {}, secretSet: {}, choice: {} };
  for (const option of schema.options) {
    const value = stored?.[option.key];
    if (option.type === 'boolean') {
      state.flags[option.key] =
        typeof value === 'boolean' ? value : option.default === true || option.default === 'true';
    } else if (option.secret) {
      state.values[option.key] = '';
      state.secretSet[option.key] = typeof value === 'string' && value !== '';
    } else {
      state.values[option.key] = value === undefined || value === null ? '' : String(value);
    }
  }
  for (const group of schema.groups) {
    const members = groupMembers(schema, group.key);
    const present = members.find((o) => filled(o, state));
    const first = members[0];
    if (present ?? first) state.choice[group.key] = (present ?? first)!.key;
  }
  return state;
}

function patternOk(pattern: string, value: string): boolean {
  try {
    return new RegExp(pattern).test(value);
  } catch {
    return true; // a pattern the browser cannot compile is left to the server
  }
}

/** Fleet's rules for the active options: required, one per group, types, ranges, patterns. */
export function validate(schema: KindSchema, state: ConnectionState): Record<string, FieldIssue> {
  const issues: Record<string, FieldIssue> = {};
  for (const option of schema.options) {
    if (!isActive(option, state) || option.type === 'boolean' || isManaged(option)) continue;
    const text = (state.values[option.key] ?? '').trim();
    if (!text) {
      if ((option.required || option.group) && !filled(option, state)) {
        issues[option.key] = { code: 'required' };
      }
      continue;
    }
    if (option.type === 'integer' || option.type === 'number') {
      const value = Number(text.replace(',', '.'));
      if (!Number.isFinite(value)) {
        issues[option.key] = { code: option.type };
      } else if (option.type === 'integer' && !Number.isInteger(value)) {
        issues[option.key] = { code: 'integer' };
      } else if (
        (option.minimum !== null && value < option.minimum) ||
        (option.maximum !== null && value > option.maximum)
      ) {
        issues[option.key] = { code: 'range' };
      }
      continue;
    }
    if (option.pattern && !patternOk(option.pattern, text)) {
      issues[option.key] = { code: 'pattern' };
    }
  }
  return issues;
}

/**
 * The `connection_options` to send. Keys the schema does not describe are kept from `stored`
 * when the kind did not change (the simulator's settings, adapter specific extras); managed
 * keys are never sent (Fleet keeps its own values).
 */
export function buildOptions(
  schema: KindSchema,
  state: ConnectionState,
  stored: Record<string, unknown> | null,
): Record<string, unknown> {
  const known = new Set(schema.options.map((o) => o.key));
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(stored ?? {})) {
    if (!known.has(key)) result[key] = value;
  }
  for (const option of schema.options) {
    if (!isActive(option, state) || isManaged(option)) continue;
    if (option.type === 'boolean') {
      result[option.key] = state.flags[option.key] ?? false;
      continue;
    }
    const text = (state.values[option.key] ?? '').trim();
    if (option.secret) {
      if (text) result[option.key] = text;
      else if (state.secretSet[option.key]) result[option.key] = SECRET_MASK;
      continue;
    }
    if (!text) continue;
    result[option.key] =
      option.type === 'integer' || option.type === 'number' ? Number(text.replace(',', '.')) : text;
  }
  return result;
}

const KNOWN_CODES: Record<string, IssueCode> = {
  required: 'required',
  one_of: 'required',
  pattern: 'pattern',
  range: 'range',
  type: 'pattern',
};

/** Fleet's 422 field errors (`#/connection_options/<key>`) as issues per option key. */
export function serverIssues(
  schema: KindSchema,
  problem: ApiProblem | null,
): Record<string, FieldIssue> {
  const issues: Record<string, FieldIssue> = {};
  if (!problem) return issues;
  for (const error of problem.errors) {
    const pointer = error.pointer ?? '';
    const match = /^#?\/connection_options\/(.+)$/.exec(pointer);
    if (!match) continue;
    const key = match[1]!;
    const option = schema.options.find((o) => o.key === key);
    const code = error.code ? KNOWN_CODES[error.code] : undefined;
    issues[key] = code && option ? { code } : { code: 'server', message: error.message };
  }
  return issues;
}

/** The issue to show for an option of a group: the group's error lands on the chosen member. */
export function issueFor(
  schema: KindSchema,
  option: OptionSchema,
  issues: Record<string, FieldIssue>,
): FieldIssue | null {
  const own = issues[option.key];
  if (own) return own;
  if (!option.group) return null;
  for (const member of groupMembers(schema, option.group)) {
    if (issues[member.key]) return issues[member.key]!;
  }
  return null;
}

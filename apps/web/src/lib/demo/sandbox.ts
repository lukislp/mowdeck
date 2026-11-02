/**
 * Demo sandboxes (ADR 0076) in the web app: pure helpers of the sign-in page and the demo band.
 *
 * With sandboxes, "Try the demo" gives every visitor an own account with own simulated mowers.
 * Identity deletes it some minutes after the last session refresh, right after a logout, and with
 * every reset of the demo; the refresh cookie of the browser then holds nothing. So that a visitor
 * who comes back (or whose tab outlived the sandbox) does not end up on an error, the browser
 * remembers that it is a demo visitor (`mowdeck.demo.visitor`): the sign-in page then starts a
 * fresh sandbox by itself. An explicit logout forgets it, so the visitor sees the normal entry.
 *
 * "Try the demo" can be refused while every sandbox is taken (503 `demo-full`) or when one address
 * started too many of them (429 `too-many-requests`); both carry `Retry-After`.
 */

/** `localStorage` key: this browser started a demo sandbox and has not logged out since. */
export const VISITOR_KEY = 'mowdeck.demo.visitor';

/** Problem type of "every sandbox is in use" (503 with `Retry-After`). */
export const DEMO_FULL_PROBLEM = 'urn:mowdeck:problem:demo-full';
/** Problem type of too many sandboxes from one address (429 with `Retry-After`). */
export const TOO_MANY_PROBLEM = 'urn:mowdeck:problem:too-many-requests';

/** Seconds to wait when a full demo sent no usable `Retry-After`. */
export const FULL_RETRY_FALLBACK_S = 120;
/** Seconds to wait when a 429 sent no usable `Retry-After`. */
export const BUSY_RETRY_FALLBACK_S = 60;

/** The part of `Storage` the visitor flag needs. */
export type VisitorStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

function browserStorage(): VisitorStorage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

/** Remember that this browser is a demo visitor (after a successful "Try the demo"). */
export function markVisitor(storage: VisitorStorage | null = browserStorage()): void {
  try {
    storage?.setItem(VISITOR_KEY, '1');
  } catch {
    // storage blocked: a lost sandbox then shows the normal demo entry
  }
}

/** Forget it (an explicit logout: the visitor left on purpose). */
export function forgetVisitor(storage: VisitorStorage | null = browserStorage()): void {
  try {
    storage?.removeItem(VISITOR_KEY);
  } catch {
    // nothing stored
  }
}

/** Whether this browser started a sandbox and has not logged out since. */
export function isVisitor(storage: VisitorStorage | null = browserStorage()): boolean {
  try {
    return storage?.getItem(VISITOR_KEY) === '1';
  } catch {
    return false;
  }
}

export interface AutoStartInput {
  /** The demo runs with sandboxes. */
  sandbox: boolean;
  /** This browser is a demo visitor ({@link isVisitor}). */
  visitor: boolean;
  /** The session of the tab: only an anonymous one (restored and found gone) restarts. */
  status: 'unknown' | 'anonymous' | 'authenticated';
  /** The sign-in page was opened for the native app's hand-off: never start by itself there. */
  appRequest: boolean;
  /** Already tried on this page (once per page; a failure shows its own state). */
  tried: boolean;
}

/** Whether the sign-in page starts a fresh sandbox by itself (the old one is gone). */
export function shouldRecreate(input: AutoStartInput): boolean {
  return (
    input.sandbox &&
    input.visitor &&
    input.status === 'anonymous' &&
    !input.appRequest &&
    !input.tried
  );
}

/** Why "Try the demo" was refused, with the seconds to wait; null for any other failure. */
export type DemoRefusal = { kind: 'full'; seconds: number } | { kind: 'busy'; seconds: number };

/** Read a refusal of `POST /identity/demo/session`. */
export function demoRefusal(problem: {
  type?: string;
  status: number;
  retryAfter?: number | null;
}): DemoRefusal | null {
  const wait = (fallback: number) =>
    typeof problem.retryAfter === 'number' && problem.retryAfter > 0
      ? Math.ceil(problem.retryAfter)
      : fallback;
  if (problem.type === DEMO_FULL_PROBLEM) {
    return { kind: 'full', seconds: wait(FULL_RETRY_FALLBACK_S) };
  }
  if (problem.status === 429 || problem.type === TOO_MANY_PROBLEM) {
    return { kind: 'busy', seconds: wait(BUSY_RETRY_FALLBACK_S) };
  }
  return null;
}

/** Seconds as the countdown readout: `mm:ss` (never negative). */
export function clock(seconds: number): string {
  const whole = Math.max(0, Math.ceil(seconds));
  const minutes = Math.floor(whole / 60);
  const rest = whole % 60;
  return `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
}

/**
 * The remaining time as screen readers hear it: started minutes (1 means "under a minute"), so
 * the live region speaks once a minute at most, not on every tick of the readout.
 */
export function spokenMinutes(seconds: number): number {
  return Math.max(1, Math.ceil(Math.max(0, Math.ceil(seconds)) / 60));
}

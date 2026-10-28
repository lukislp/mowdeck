/**
 * Polling with backoff for the tracking views. The base interval applies while requests succeed;
 * failures double the wait (up to a minute) and a `Retry-After` from the server (HTTP 429) is
 * honoured (up to five minutes) so a rate-limited public page does not hammer the endpoint.
 */

export const POLL_INTERVAL_MS = 8_000;
const MAX_BACKOFF_MS = 60_000;
const MAX_RETRY_AFTER_MS = 300_000;

/** The wait before the next request. `failures` counts consecutive failed requests. */
export function nextDelay(
  failures: number,
  retryAfterSeconds: number | null = null,
  base = POLL_INTERVAL_MS,
): number {
  const backoff =
    failures > 0 ? Math.min(base * 2 ** Math.min(failures, 10), MAX_BACKOFF_MS) : base;
  if (retryAfterSeconds === null || !Number.isFinite(retryAfterSeconds) || retryAfterSeconds <= 0) {
    return backoff;
  }
  return Math.max(backoff, Math.min(retryAfterSeconds * 1000, MAX_RETRY_AFTER_MS));
}

export interface PollOutcome {
  /** The request failed (network, server error or rate limit). */
  failed?: boolean;
  /** Seconds from `Retry-After`, if the server sent one. */
  retryAfter?: number | null;
  /** Nothing left to poll (session ended, link unusable). */
  stop?: boolean;
}

/**
 * Runs `task` now and then again after each delay until it reports `stop` or the returned
 * function is called. A task that throws counts as a failure.
 */
export function startPoller(
  task: () => Promise<PollOutcome | void>,
  base = POLL_INTERVAL_MS,
): () => void {
  let stopped = false;
  let failures = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const run = async (): Promise<void> => {
    let outcome: PollOutcome;
    try {
      outcome = (await task()) ?? {};
    } catch {
      outcome = { failed: true };
    }
    if (stopped || outcome.stop) return;
    failures = outcome.failed ? failures + 1 : 0;
    timer = setTimeout(() => void run(), nextDelay(failures, outcome.retryAfter ?? null, base));
  };

  void run();
  return () => {
    stopped = true;
    clearTimeout(timer);
  };
}

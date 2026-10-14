/** A coarse clock for "x s ago" texts: one shared tick, so a list of 50 ages costs one timer. */
class Clock {
  now = $state(Date.now());
  #holders = 0;
  #timer: ReturnType<typeof setInterval> | undefined;

  /** Keep ticking while held; returns the release function (use in `$effect`). */
  hold(intervalMs = 5_000): () => void {
    this.#holders += 1;
    this.now = Date.now();
    if (this.#holders === 1) this.#timer = setInterval(() => (this.now = Date.now()), intervalMs);
    return () => {
      this.#holders -= 1;
      if (this.#holders === 0) clearInterval(this.#timer);
    };
  }
}

export const clock = new Clock();

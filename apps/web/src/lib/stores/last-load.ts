/**
 * The last successful answer of a page loader, kept in memory for this tab. A page shows it at
 * once when it is opened again (and while its fresh load runs or fails), instead of a blank
 * "loading" screen. Never persisted; cleared on logout.
 */
const answers = new Map<string, unknown>();

export const lastLoad = {
  get<T>(key: string): T | undefined {
    return answers.get(key) as T | undefined;
  },
  set<T>(key: string, value: T): void {
    answers.set(key, value);
  },
  clear(): void {
    answers.clear();
  },
};

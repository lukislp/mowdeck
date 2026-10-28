/**
 * JSON lines on stdout, like the Python services. Fields are passed explicitly: there is no way
 * to log a whole object by accident, and secrets (tokens, passcodes, pairing codes) never are.
 */
const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 } as const;
export type Level = keyof typeof LEVELS;
type Fields = Record<string, string | number | boolean | null | undefined>;

let minimum: Level = 'info';

export function setLevel(level: Level): void {
  minimum = level;
}

export function log(level: Level, event: string, fields: Fields = {}): void {
  if (LEVELS[level] < LEVELS[minimum]) return;
  const line = JSON.stringify({
    timestamp: new Date().toISOString(),
    level,
    service: 'matter-server',
    event,
    ...fields,
  });
  (level === 'error' || level === 'warn' ? process.stderr : process.stdout).write(`${line}\n`);
}

/** A short, secret-free description of an error. */
export function errorText(error: unknown): string {
  if (error instanceof Error) return `${error.name}: ${error.message}`.slice(0, 300);
  return String(error).slice(0, 300);
}

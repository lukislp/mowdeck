/**
 * Vendor error codes in plain words (ADR 0067). The texts come from the vendor's error lexicon,
 * which ships with its adapter package and is served by Telemetry; the app holds no code table
 * of its own. Only the words around the explanation (severity, labels) are translated here.
 */
import { telemetryApi, unwrap } from '#lib/api/client.js';
import type { components } from '#lib/generated/telemetry.js';
import { m } from '#lib/paraglide/messages.js';

type Schemas = components['schemas'];
export type ErrorExplanation = Schemas['ErrorExplanation'];
export type ErrorSeverity = Schemas['ErrorSeverity'];
export type MowerErrorOut = Schemas['MowerErrorOut'];

/**
 * The current error of a mower, explained in `locale` (null `error`: none). `fresh` skips the
 * browser's copy (Telemetry lets it keep the answer for 15 s).
 */
export function getMowerError(
  mowerId: string,
  locale: string,
  fresh = false,
): Promise<MowerErrorOut> {
  return unwrap(
    telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/error', {
      params: { path: { mower_id: mowerId }, query: { locale } },
      ...(fresh ? { cache: 'no-cache' as const } : {}),
    }),
  );
}

/**
 * Explain the error `code` the live state shows. The live state (Realtime) and Telemetry's
 * stored state come from the same bus message, but Telemetry may not have stored it yet when the
 * page asks: while its answer does not carry `code`, ask again (fresh) a few times. Null when the
 * mower reports no error, the answer never caught up, or `signal` aborted.
 */
export async function explainCurrentError(
  mowerId: string,
  code: string,
  locale: string,
  signal: AbortSignal,
  { retries = 5, delayMs = 1_000 }: { retries?: number; delayMs?: number } = {},
): Promise<ErrorExplanation | null> {
  for (let attempt = 0; !signal.aborted; attempt += 1) {
    const answer = await getMowerError(mowerId, locale, attempt > 0);
    const error = answer.error?.code ? answer.error : null;
    if (error?.code === code.trim() || attempt >= retries) return error;
    await new Promise((done) => setTimeout(done, delayMs));
  }
  return null;
}

/** Explain several codes of one mower (its vendor's lexicon), in the order given. */
export async function explainMowerCodes(
  mowerId: string,
  codes: string[],
  locale: string,
): Promise<ErrorExplanation[]> {
  if (codes.length === 0) return [];
  const body = await unwrap(
    telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/error-codes', {
      params: { path: { mower_id: mowerId }, query: { code: codes.slice(0, 20), locale } },
    }),
  );
  return body.explanations;
}

export function severityLabel(severity: ErrorSeverity): string {
  switch (severity) {
    case 'info':
      return m.errx_severity_info();
    case 'warning':
      return m.errx_severity_warning();
    case 'error':
      return m.errx_severity_error();
    case 'critical':
      return m.errx_severity_critical();
  }
}

/** The tone of a severity: the mower state colour the card borrows. */
export function severityTone(severity: ErrorSeverity): 'error' | 'paused' | 'unknown' {
  switch (severity) {
    case 'critical':
    case 'error':
      return 'error';
    case 'warning':
      return 'paused';
    case 'info':
      return 'unknown';
  }
}

/**
 * The key an explanation is loaded for: it changes when the mower reports another code, so the
 * page asks again only then (and when the language changes).
 */
export function explainKey(
  mowerId: string | undefined,
  code: string | null | undefined,
  locale: string,
): string | null {
  return mowerId && code ? `${mowerId}|${code}|${locale}` : null;
}

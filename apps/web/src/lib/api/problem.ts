/** RFC 9457 problem details as the services send them (``application/problem+json``). */

export interface ProblemFieldError {
  pointer?: string | null;
  message: string;
  /** Machine readable code of the rule, e.g. `required` or `pattern`. */
  code?: string | null;
  /** The offending parameter (a query parameter, or the plan limit or feature of a refusal). */
  parameter?: string | null;
}

export const PROBLEM_PREFIX = 'urn:mowdeck:problem:';

/** A failed API call. `slug` is the part after `urn:mowdeck:problem:` (e.g. `not-found`). */
export class ApiProblem extends Error {
  readonly type: string;
  readonly status: number;
  readonly title: string;
  readonly detail: string | null;
  readonly correlationId: string | null;
  readonly errors: ProblemFieldError[];
  /** Seconds from `Retry-After` (429, 503), if the response had one. */
  readonly retryAfter: number | null;

  constructor(init: {
    type?: string;
    status: number;
    title?: string;
    detail?: string | null;
    correlationId?: string | null;
    errors?: ProblemFieldError[];
    retryAfter?: number | null;
  }) {
    super(init.detail ?? init.title ?? `HTTP ${init.status}`);
    this.name = 'ApiProblem';
    this.type = init.type ?? 'about:blank';
    this.status = init.status;
    this.title = init.title ?? '';
    this.detail = init.detail ?? null;
    this.correlationId = init.correlationId ?? null;
    this.errors = init.errors ?? [];
    this.retryAfter = init.retryAfter ?? null;
  }

  get slug(): string {
    return this.type.startsWith(PROBLEM_PREFIX) ? this.type.slice(PROBLEM_PREFIX.length) : '';
  }

  /** The message of the field error whose pointer ends with `/field`, if any. */
  fieldError(field: string): string | null {
    return this.fieldErrorOf(field)?.message ?? null;
  }

  /** The field error whose pointer ends with `/field`, if any. */
  fieldErrorOf(field: string): ProblemFieldError | null {
    return this.errors.find((e) => (e.pointer ?? '').split('/').pop() === field) ?? null;
  }

  /** A network failure (no HTTP response). */
  static network(): ApiProblem {
    return new ApiProblem({ type: `${PROBLEM_PREFIX}network`, status: 0 });
  }
}

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

/** Build an {@link ApiProblem} from a parsed error body (problem+json or anything else). */
export function problemFromBody(status: number, raw: unknown, headers?: Headers): ApiProblem {
  const body: Record<string, unknown> =
    raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
  const errors = Array.isArray(body.errors)
    ? (body.errors as unknown[]).flatMap((e) =>
        e && typeof e === 'object' && 'detail' in e
          ? [
              {
                pointer: String((e as { pointer?: unknown }).pointer ?? ''),
                message: String((e as { detail: unknown }).detail),
                code: stringOrNull((e as { code?: unknown }).code),
                parameter: stringOrNull((e as { parameter?: unknown }).parameter),
              },
            ]
          : [],
      )
    : [];
  return new ApiProblem({
    type: typeof body.type === 'string' ? body.type : undefined,
    status,
    title: typeof body.title === 'string' ? body.title : undefined,
    detail: typeof body.detail === 'string' ? body.detail : null,
    correlationId:
      typeof body.correlation_id === 'string'
        ? body.correlation_id
        : (headers?.get('x-correlation-id') ?? null),
    errors,
    retryAfter: retryAfterSeconds(headers?.get('retry-after') ?? null),
  });
}

/** `Retry-After` as seconds (a number of seconds or an HTTP date), null when absent. */
export function retryAfterSeconds(value: string | null, now: number = Date.now()): number | null {
  if (!value) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(0, Math.ceil(seconds));
  const at = Date.parse(value);
  return Number.isNaN(at) ? null : Math.max(0, Math.ceil((at - now) / 1000));
}

/** Build an {@link ApiProblem} from a non-2xx response whose body was not read yet. */
export async function problemFromResponse(response: Response): Promise<ApiProblem> {
  let body: unknown = null;
  try {
    body = await response.clone().json();
  } catch {
    // not JSON: keep the status only
  }
  return problemFromBody(response.status, body, response.headers);
}

/** Normalise anything thrown by an API call into an {@link ApiProblem}. */
export function asProblem(error: unknown): ApiProblem {
  if (error instanceof ApiProblem) return error;
  return ApiProblem.network();
}

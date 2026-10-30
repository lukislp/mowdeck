import { afterEach, describe, expect, it, vi } from 'vitest';

const get = vi.fn();
vi.mock('#lib/api/client.js', () => ({
  telemetryApi: { GET: (...a: unknown[]) => get(...a) },
  unwrap: async (p: Promise<unknown>) => p,
}));

import { explainCurrentError, type ErrorExplanation } from './error-explain';

const stuck = { code: 'stuck', title: 'Steckt an einem Hindernis fest' } as ErrorExplanation;

function answer(error: ErrorExplanation | null) {
  return Promise.resolve({ mower_id: 'm1', observed_at: '', activity: 'error', error });
}

afterEach(() => get.mockReset());

describe('explainCurrentError', () => {
  it('asks once when Telemetry already has the code', async () => {
    get.mockReturnValueOnce(answer(stuck));
    const error = await explainCurrentError('m1', 'stuck', 'de', new AbortController().signal);
    expect(error).toBe(stuck);
    expect(get).toHaveBeenCalledTimes(1);
    expect(get.mock.calls[0]![1]).not.toHaveProperty('cache');
  });

  it('asks again, past the browser cache, until Telemetry stored the state the page shows', async () => {
    get.mockReturnValueOnce(answer(null)).mockReturnValueOnce(answer(stuck));
    const error = await explainCurrentError('m1', 'stuck', 'de', new AbortController().signal, {
      delayMs: 0,
    });
    expect(error).toBe(stuck);
    expect(get).toHaveBeenCalledTimes(2);
    expect(get.mock.calls[1]![1]).toMatchObject({ cache: 'no-cache' });
  });

  it('gives up after the retries with the last answer', async () => {
    get.mockImplementation(() => answer(null));
    const error = await explainCurrentError('m1', 'stuck', 'de', new AbortController().signal, {
      retries: 2,
      delayMs: 0,
    });
    expect(error).toBeNull();
    expect(get).toHaveBeenCalledTimes(3);
  });

  it('stops asking once the page moved on', async () => {
    const asked = new AbortController();
    get.mockImplementation(() => {
      asked.abort();
      return answer(null);
    });
    expect(await explainCurrentError('m1', 'stuck', 'de', asked.signal, { delayMs: 0 })).toBeNull();
    expect(get).toHaveBeenCalledTimes(1);
  });
});

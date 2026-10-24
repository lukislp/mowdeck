import { beforeEach, describe, expect, it, vi } from 'vitest';

const goto = vi.fn<(path: string, options?: object) => Promise<void>>();
const match = vi.fn<(path: string) => Promise<object | null>>();

vi.mock('$app/navigation', () => ({ goto }));
vi.mock('$app/paths', () => ({ match }));

const { navigatePath } = await import('./navigate.js');

describe('navigatePath', () => {
  const assign = vi.fn();
  const replace = vi.fn();

  beforeEach(() => {
    goto.mockReset().mockResolvedValue(undefined);
    match.mockReset();
    assign.mockReset();
    replace.mockReset();
    vi.stubGlobal('location', { assign, replace });
  });

  it('routes a known path on the client with the given options', async () => {
    match.mockResolvedValue({ id: '/(app)/mowers/[id]', params: { id: 'm1' } });
    await navigatePath('/mowers/m1?tab=map', { replace: true });
    expect(match).toHaveBeenCalledWith('/mowers/m1?tab=map');
    expect(goto).toHaveBeenCalledWith('/mowers/m1?tab=map', { replace: true });
    expect(assign).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });

  it('loads a path without a route as a full page', async () => {
    match.mockResolvedValue(null);
    await navigatePath('/gone/away');
    expect(goto).not.toHaveBeenCalled();
    expect(assign).toHaveBeenCalledWith('/gone/away');
  });

  it('replaces the history entry for an unknown path when asked to', async () => {
    match.mockResolvedValue(null);
    await navigatePath('/gone/away', { replace: true });
    expect(replace).toHaveBeenCalledWith('/gone/away');
    expect(assign).not.toHaveBeenCalled();
  });

  it('passes a rejection of goto on to the caller', async () => {
    match.mockResolvedValue({ id: '/login', params: {} });
    goto.mockRejectedValue(new Error('aborted'));
    await expect(navigatePath('/login')).rejects.toThrow('aborted');
  });
});

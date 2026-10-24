import { describe, expect, it, vi } from 'vitest';

import { availability, polygonArea, primaryCommand } from '#lib/mower/commands.js';
import type { AppModule } from '#lib/modules/types.js';
import { visibleModules } from '#lib/shell/registry.svelte.js';

import { authFetch } from './client';
import {
  ApiProblem,
  asProblem,
  problemFromBody,
  problemFromResponse,
  retryAfterSeconds,
} from './problem';

describe('problems', () => {
  it('parses problem+json with field errors and correlation id', async () => {
    const response = new Response(
      JSON.stringify({
        type: 'urn:mowdeck:problem:validation-failed',
        title: 'Validation failed',
        status: 422,
        detail: 'bad',
        correlation_id: 'abc',
        errors: [{ pointer: '#/name', detail: 'too long' }],
      }),
      { status: 422 },
    );
    const problem = await problemFromResponse(response);
    expect(problem.slug).toBe('validation-failed');
    expect(problem.correlationId).toBe('abc');
    expect(problem.fieldError('name')).toBe('too long');
    expect(problem.fieldError('other')).toBeNull();
  });

  it('keeps the error codes and Retry-After', async () => {
    const response = new Response(
      JSON.stringify({
        type: 'urn:mowdeck:problem:too-many-requests',
        status: 429,
        errors: [
          { pointer: '#/connection_options/host', detail: 'host is required', code: 'required' },
        ],
      }),
      { status: 429, headers: { 'Retry-After': '42' } },
    );
    const problem = await problemFromResponse(response);
    expect(problem.retryAfter).toBe(42);
    expect(problem.fieldErrorOf('host')?.code).toBe('required');
    expect(problem.errors[0]?.parameter).toBeNull();
    expect(retryAfterSeconds(null)).toBeNull();
    expect(retryAfterSeconds('soon')).toBeNull();
    const now = Date.parse('2026-10-09T12:00:00Z');
    expect(retryAfterSeconds('Fri, 09 Oct 2026 12:00:30 GMT', now)).toBe(30);
  });

  it('copes with bodies that are not problems', async () => {
    const problem = await problemFromResponse(new Response('oops', { status: 502 }));
    expect(problem.status).toBe(502);
    expect(problem.slug).toBe('');
    expect(problemFromBody(500, null).type).toBe('about:blank');
    expect(asProblem(new Error('x')).slug).toBe('network');
    expect(asProblem(new ApiProblem({ status: 404 })).status).toBe(404);
  });
});

describe('authFetch', () => {
  function tokens(fresh: string | null = 'fresh') {
    return {
      validToken: vi.fn(async (force?: boolean) => (force ? fresh : 'old')),
      expire: vi.fn(),
    };
  }

  it('adds the bearer token and a correlation id', async () => {
    const base = vi.fn(async (request: Request) => {
      expect(request.headers.get('Authorization')).toBe('Bearer old');
      expect(request.headers.get('X-Correlation-ID')).toBeTruthy();
      return new Response('{}', { status: 200 });
    });
    const response = await authFetch(tokens(), base as never)('http://x/api/v1/fleet/mowers');
    expect(response.status).toBe(200);
  });

  it('retries once after a refresh when a 401 arrives', async () => {
    const seen: string[] = [];
    const base = vi.fn(async (request: Request) => {
      seen.push(request.headers.get('Authorization') ?? '');
      return new Response('{}', { status: seen.length === 1 ? 401 : 200 });
    });
    const source = tokens();
    const response = await authFetch(source, base as never)('http://x/a', {
      method: 'POST',
      body: '{"a":1}',
    });
    expect(response.status).toBe(200);
    expect(seen).toEqual(['Bearer old', 'Bearer fresh']);
    expect(source.expire).not.toHaveBeenCalled();
  });

  it('ends the session when the refresh fails or the retry is refused', async () => {
    const unauthorized = vi.fn(async () => new Response('{}', { status: 401 }));
    const noToken = tokens(null);
    expect((await authFetch(noToken, unauthorized as never)('http://x/a')).status).toBe(401);
    expect(noToken.expire).toHaveBeenCalledOnce();
    const refused = tokens('fresh');
    await authFetch(refused, unauthorized as never)('http://x/a');
    expect(refused.expire).toHaveBeenCalledOnce();
  });

  it('turns a network failure into a problem', async () => {
    const offline = vi.fn(async () => {
      throw new TypeError('failed');
    });
    await expect(authFetch(tokens(), offline as never)('http://x/a')).rejects.toMatchObject({
      status: 0,
    });
  });
});

describe('command availability', () => {
  it('offers start at rest, pause while driving, resume when paused, dock when away', () => {
    expect(availability('start', 'docked').enabled).toBe(true);
    expect(availability('start', 'mowing')).toEqual({ enabled: false, reason: 'start' });
    expect(availability('pause', 'returning').enabled).toBe(true);
    expect(availability('resume', 'paused').enabled).toBe(true);
    expect(availability('dock', 'charging')).toEqual({ enabled: false, reason: 'dock' });
    expect(availability('dock', 'mowing').enabled).toBe(true);
    expect(availability('start', 'offline')).toEqual({ enabled: false, reason: 'offline' });
    expect(availability('pause', null).reason).toBe('offline');
  });

  it('names the main action of each state', () => {
    expect(primaryCommand('docked')).toBe('start');
    expect(primaryCommand('mowing')).toBe('pause');
    expect(primaryCommand('paused')).toBe('resume');
    expect(primaryCommand('error')).toBe('dock');
    expect(primaryCommand('offline')).toBeNull();
  });

  it('measures polygon areas', () => {
    expect(
      polygonArea([
        [0, 0],
        [12, 0],
        [12, 8],
        [0, 8],
      ]),
    ).toBe(96);
  });
});

describe('module registry', () => {
  const module = (
    id: string,
    route: string,
    permission: string | null,
    core = false,
  ): AppModule => ({
    id,
    route,
    label: () => id,
    icon: 'mower',
    requiredPermission: permission,
    order: 1,
    core,
  });
  const modules = [
    module('mowers', '/mowers', 'mower.view'),
    module('users', '/settings/users', 'users.manage'),
    module('account', '/account', null, true),
  ];

  it('needs the permission and an announced area unless core', () => {
    const areas = [{ route: '/mowers', module: 'fleet', required_permission: 'mower.view' }];
    const ids = (list: AppModule[]) => list.map((m) => m.id);
    expect(ids(visibleModules(modules, ['mower.view', 'users.manage'], areas))).toEqual([
      'mowers',
      'account',
    ]);
    expect(ids(visibleModules(modules, ['mower.view'], []))).toEqual(['account']);
  });

  it('shows operator modules only to operator admins', () => {
    const operator: AppModule = {
      ...module('operator', '/operator', null, true),
      platformAdmin: true,
    };
    const all = [...modules, operator];
    expect(visibleModules(all, [], null).map((m) => m.id)).toEqual(['account']);
    expect(visibleModules(all, [], null, true).map((m) => m.id)).toEqual(['account', 'operator']);
  });

  it('falls back to the permission check when the areas are unknown', () => {
    expect(visibleModules(modules, ['users.manage'], null).map((m) => m.id)).toEqual([
      'users',
      'account',
    ]);
  });
});

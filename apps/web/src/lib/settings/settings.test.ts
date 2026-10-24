import { describe, expect, it } from 'vitest';

import type { AppModule } from '#lib/modules/types.js';
import { builtIn, navModules, visibleModules } from '#lib/shell/registry.svelte.js';

import { forwardTarget, sectionHref, settingsSections } from './sections';

const mod = (over: Partial<AppModule> & { id: string; route: string }): AppModule => ({
  label: () => over.id,
  icon: 'user',
  requiredPermission: null,
  order: 50,
  ...over,
});

const modules: AppModule[] = [
  mod({ id: 'mowers', route: '/mowers', order: 10 }),
  mod({ id: 'settings', route: '/settings', order: 90, core: true }),
  mod({
    id: 'tokens',
    route: '/settings/api-tokens',
    order: 60,
    core: true,
    placement: 'settings',
    section: 'api-tokens',
  }),
  mod({
    id: 'account',
    route: '/account',
    order: 5,
    core: true,
    placement: 'settings',
    section: 'profile',
  }),
  mod({
    id: 'notif',
    route: '/notifications/settings',
    area: '/notifications',
    order: 21,
    requiredPermission: 'mower.view',
    placement: 'settings',
    section: 'notifications',
  }),
];

describe('settings sections', () => {
  it('lists the settings modules in module order with their anchors', () => {
    expect(settingsSections(modules).map((s) => s.anchor)).toEqual([
      'profile',
      'notifications',
      'api-tokens',
    ]);
  });

  it('keeps settings sections out of the navigation', () => {
    expect(navModules(modules).map((m) => m.id)).toEqual(['mowers', 'settings']);
  });

  it('enables a section through the ui_area of another route', () => {
    const area = (route: string) => [{ route, module: 'x', required_permission: 'mower.view' }];
    const shown = (areas: ReturnType<typeof area>) =>
      settingsSections(visibleModules(modules, ['mower.view'], areas)).map((s) => s.anchor);
    expect(shown(area('/notifications'))).toContain('notifications');
    expect(shown(area('/notifications/settings'))).not.toContain('notifications');
    expect(shown([])).toEqual(['profile', 'api-tokens']);
  });

  it('builds section addresses with the query kept', () => {
    expect(sectionHref('profile')).toBe('/settings#profile');
    expect(sectionHref('profile', '?password=1')).toBe('/settings?password=1#profile');
    expect(sectionHref('profile', 'password=1')).toBe('/settings?password=1#profile');
    expect(sectionHref('profile', '?')).toBe('/settings#profile');
  });

  it('forwards the old routes to their anchors', () => {
    expect(forwardTarget(modules, '/settings/api-tokens')).toBe('/settings#api-tokens');
    expect(forwardTarget(modules, '/settings/api-tokens/')).toBe('/settings#api-tokens');
    expect(forwardTarget(modules, '/notifications/settings')).toBe('/settings#notifications');
    expect(forwardTarget(modules, '/account', '?billing=success', '#plan')).toBe(
      '/settings?billing=success#plan',
    );
    expect(forwardTarget(modules, '/account', '?password=1')).toBe('/settings?password=1#profile');
    expect(forwardTarget(modules, '/mowers')).toBeNull();
    expect(forwardTarget(modules, '/settings')).toBeNull();
  });

  it('covers every old settings route of the built-in modules', () => {
    const targets = [
      '/account',
      '/notifications/settings',
      '/settings/users',
      '/settings/ai-provider',
      '/settings/linked-accounts',
      '/settings/api-tokens',
    ].map((path) => forwardTarget(builtIn, path));
    expect(targets).toEqual([
      '/settings#profile',
      '/settings#notifications',
      '/settings#users',
      '/settings#ai-provider',
      '/settings#linked-accounts',
      '/settings#api-tokens',
    ]);
  });

  it('leaves six entries in the navigation of an owner', () => {
    const owner = ['mower.view', 'users.manage', 'fleet.manage', 'mower.drive'];
    expect(navModules(visibleModules(builtIn, owner, null)).map((m) => m.id)).toEqual([
      'mowers',
      'fleet',
      'schedules',
      'notifications',
      'reports',
      'settings',
    ]);
  });
});

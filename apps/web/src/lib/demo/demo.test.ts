// @vitest-environment node
/**
 * The public demo (ADR 0068) in the runtime config: the `demo` block of /config.json as
 * nginx/40-mowdeck-runtime-config.sh renders it, and as the app reads it.
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { DEFAULT_CONFIG, parseConfig } from '#lib/config.js';

import { demoTitle, isDemoProblem } from './demo';

const script = join(process.cwd(), 'nginx', '40-mowdeck-runtime-config.sh');
const hasSh = spawnSync('sh', ['-c', 'exit 0']).status === 0;

function render(env: Record<string, string>): Record<string, unknown> {
  const dir = mkdtempSync(join(tmpdir(), 'mowdeck-demo-'));
  const template = join(dir, 'template.conf');
  writeFileSync(template, 'add_header Content-Security-Policy "default-src \'self\'" always;\n');
  const result = spawnSync('sh', [script], {
    env: {
      PATH: process.env.PATH ?? '',
      MOWDECK_CONFIG_DIR: join(dir, 'out'),
      MOWDECK_CSP_TEMPLATE: template,
      ...env,
    },
    encoding: 'utf8',
  });
  expect(result.status, result.stderr).toBe(0);
  return JSON.parse(readFileSync(join(dir, 'out', 'config.json'), 'utf8'));
}

describe('parseConfig demo', () => {
  it('has no demo unless the block is there', () => {
    expect(DEFAULT_CONFIG.demo).toBeUndefined();
    expect(parseConfig({}).demo).toBeUndefined();
    expect(parseConfig({ demo: true }).demo).toBeUndefined();
    expect(parseConfig({ demo: null }).demo).toBeUndefined();
  });

  it('reads the reset interval and falls back to six hours', () => {
    expect(parseConfig({ demo: { resetHours: 4 } }).demo).toEqual({ resetHours: 4 });
    expect(parseConfig({ demo: {} }).demo).toEqual({ resetHours: 6 });
    expect(parseConfig({ demo: { resetHours: 0 } }).demo).toEqual({ resetHours: 6 });
    expect(parseConfig({ demo: { resetHours: 2.5 } }).demo).toEqual({ resetHours: 6 });
    expect(parseConfig({ demo: { resetHours: 1000 } }).demo).toEqual({ resetHours: 168 });
  });
});

describe.skipIf(!hasSh)('rendered demo block', () => {
  it('is absent on a normal installation', () => {
    expect(render({})).not.toHaveProperty('demo');
    expect(render({ MOWDECK_WEB_DEMO_MODE: 'false' })).not.toHaveProperty('demo');
  });

  it('carries the reset interval in a demo', () => {
    const config = render({ MOWDECK_WEB_DEMO_MODE: 'true', MOWDECK_WEB_DEMO_RESET_HOURS: '4' });
    expect(config.demo).toEqual({ resetHours: 4 });
    expect(parseConfig(config).demo).toEqual({ resetHours: 4 });
  });

  it('replaces an unusable interval', () => {
    for (const hours of ['', 'abc', '0', '999', '-2']) {
      const config = render({ MOWDECK_WEB_DEMO_MODE: 'on', MOWDECK_WEB_DEMO_RESET_HOURS: hours });
      expect(config.demo).toEqual({ resetHours: 6 });
    }
  });
});

describe('demo helpers', () => {
  it('prefixes a title once', () => {
    expect(demoTitle('Mowers · MowDeck', 'Demo')).toBe('Demo · Mowers · MowDeck');
    expect(demoTitle('Demo · Mowers · MowDeck', 'Demo')).toBe('Demo · Mowers · MowDeck');
    expect(demoTitle('', 'Demo')).toBe('Demo');
  });

  it('recognises the demo problem', () => {
    expect(isDemoProblem({ type: 'urn:mowdeck:problem:demo-mode' })).toBe(true);
    expect(isDemoProblem({ type: 'urn:mowdeck:problem:forbidden' })).toBe(false);
    expect(isDemoProblem(null)).toBe(false);
  });
});

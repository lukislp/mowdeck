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
  return JSON.parse(readFileSync(join(renderTo(env), 'config.json'), 'utf8'));
}

/** The nginx rules that label the served HTML (demo.conf). */
function renderNginx(env: Record<string, string>): string {
  return readFileSync(join(renderTo(env), 'demo.conf'), 'utf8');
}

/** Run the runtime config script; the directory it rendered into. */
function renderTo(env: Record<string, string>): string {
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
  return join(dir, 'out');
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

describe.skipIf(!hasSh)('demo label in the served HTML (demo.conf)', () => {
  const appHtml = readFileSync(join(process.cwd(), 'src', 'app.html'), 'utf8');

  it('leaves the page of a normal installation as built', () => {
    const envs: Record<string, string>[] = [{}, { MOWDECK_WEB_DEMO_MODE: 'false' }];
    for (const env of envs) {
      const conf = renderNginx(env);
      expect(conf).not.toMatch(/^\s*sub_filter/m);
      expect(conf.trim().startsWith('#')).toBe(true);
    }
  });

  it('puts "Demo" into the title, description and Open Graph tags of a demo', () => {
    const conf = renderNginx({ MOWDECK_WEB_DEMO_MODE: 'true', MOWDECK_WEB_DEMO_RESET_HOURS: '4' });
    const rule = /^sub_filter '([^']+)' '([^']+)';$/m.exec(conf);
    expect(rule, conf).not.toBeNull();
    const [, find, replace] = rule ?? [];
    // The string it looks for is exactly the title of app.html, so the rule cannot miss silently.
    expect(appHtml).toContain(find);
    expect(replace).toContain('<title>Demo · MowDeck</title>');
    expect(replace).toContain('<meta property="og:title" content="Demo · MowDeck" />');
    expect(replace).toContain('<meta property="og:site_name" content="MowDeck Demo" />');
    expect(replace).toMatch(/<meta name="description" content="[^"]*Demo[^"]*4 h[^"]*" \/>/);
    // Text in <head> only: no script, so the build's CSP hashes stay valid.
    expect(replace).not.toMatch(/<script|\son\w+=|\sstyle=/i);
    expect(demoTitle('Demo · MowDeck', 'Demo')).toBe('Demo · MowDeck');
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

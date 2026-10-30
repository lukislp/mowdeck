// @vitest-environment node
import { mkdtempSync, readFileSync, readdirSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  explorerDocuments,
  explorerFiles,
  integrity,
  pinnedVersion,
  scalarPackage,
  writeExplorer,
} from './api-explorer.mjs';

const webRoot = process.cwd();

describe('the frame build', () => {
  const files = explorerFiles(webRoot);

  it('ships the pinned Scalar bundle, never a range', () => {
    const pinned = pinnedVersion(webRoot);
    expect(pinned).toMatch(/^\d+\.\d+\.\d+$/);
    expect(scalarPackage(webRoot).version).toBe(pinned);
    expect(String(files.get('LICENSE.txt'))).toContain(`@scalar/api-reference ${pinned} (MIT)`);
  });

  it('loads every script and style from this origin, with integrity', () => {
    const page = String(files.get('index.html'));
    expect(page).not.toMatch(/%[A-Z_]+%/);
    expect(page).not.toMatch(/(src|href)="(https?:)?\/\//);
    expect(page).not.toMatch(/<script(?![^>]*\bsrc=)[^>]*>/);
    expect(page).not.toMatch(/<style|style="/);
    expect(page).toContain(`integrity="${integrity(files.get('scalar.js')!)}"`);
    expect(page).toContain(`integrity="${integrity(files.get('explorer.js')!)}"`);
    expect(page).toContain(`integrity="${integrity(files.get('explorer.css')!)}"`);
  });

  it('lists the services with operations, Fleet first', () => {
    const index = JSON.parse(String(files.get('openapi/index.json'))) as { slug: string }[];
    const slugs = index.map((entry) => entry.slug);
    expect(slugs[0]).toBe('fleet');
    expect(slugs).toContain('identity');
    expect(slugs).not.toContain('realtime');
    for (const slug of slugs) expect(files.has(`openapi/${slug}.json`)).toBe(true);
  });

  it('writes the frame next to the app', () => {
    const out = mkdtempSync(join(tmpdir(), 'mowdeck-explorer-'));
    const { target, count } = writeExplorer(out, webRoot);
    expect(target).toBe(join(out, '_explorer'));
    expect(readdirSync(target)).toEqual(
      expect.arrayContaining(['index.html', 'scalar.js', 'explorer.js', 'frame.js', 'openapi']),
    );
    expect(count).toBe(files.size);
  });
});

describe('explorerDocuments', () => {
  it('orders the known services first and skips documents without the public API', () => {
    const dir = mkdtempSync(join(tmpdir(), 'mowdeck-openapi-'));
    mkdirSync(dir, { recursive: true });
    const doc = (paths: string[]) =>
      JSON.stringify({ paths: Object.fromEntries(paths.map((p) => [p, {}])) });
    writeFileSync(join(dir, 'billing.json'), doc(['/api/v1/billing/plans']));
    writeFileSync(join(dir, 'telemetry.json'), doc(['/api/v1/telemetry/state']));
    writeFileSync(join(dir, 'fleet.json'), doc(['/api/v1/fleet/mowers']));
    writeFileSync(join(dir, 'realtime.json'), doc(['/.well-known/mowdeck-module']));
    writeFileSync(join(dir, 'device-gateway.json'), doc(['/api/v1/device-gateway/links']));
    writeFileSync(join(dir, 'notes.txt'), 'x');
    const docs = explorerDocuments(dir);
    expect(docs.map((d) => [d.slug, d.title])).toEqual([
      ['fleet', 'Fleet'],
      ['telemetry', 'Telemetry'],
      ['billing', 'Billing'],
      ['device-gateway', 'Device Gateway'],
    ]);
    expect(readFileSync(join(dir, 'fleet.json'), 'utf8')).toContain('/api/v1/fleet/mowers');
  });
});

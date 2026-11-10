// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { PRIVACY_URL, PROJECT_URL } from './project-links';

const html = readFileSync(resolve(import.meta.dirname, '../index.html'), 'utf8');

function anchor(id: string): string {
  const match = new RegExp(String.raw`<a\s+id="${id}"[^>]*>`).exec(html);
  expect(match, `#${id} in index.html`).not.toBeNull();
  return match![0];
}

describe('the links of the first-launch screen', () => {
  it('are https and match the constants', () => {
    expect(PROJECT_URL.startsWith('https://')).toBe(true);
    expect(PRIVACY_URL.startsWith(`${PROJECT_URL}/`)).toBe(true);
    expect(anchor('about')).toContain(`href="${PROJECT_URL}"`);
    expect(anchor('privacy')).toContain(`href="${PRIVACY_URL}"`);
  });

  it('open outside the shell', () => {
    for (const id of ['about', 'privacy']) {
      expect(anchor(id)).toContain('target="_blank"');
      expect(anchor(id)).toContain('rel="noopener noreferrer"');
    }
  });
});

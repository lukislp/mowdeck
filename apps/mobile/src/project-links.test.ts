// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { PRIVACY_POLICY_URLS, privacyPolicyUrl, PROJECT_URL } from './project-links';

const html = readFileSync(resolve(import.meta.dirname, '../index.html'), 'utf8');

function anchor(id: string): string {
  const match = new RegExp(String.raw`<a\s+id="${id}"[^>]*>`).exec(html);
  expect(match, `#${id} in index.html`).not.toBeNull();
  return match![0];
}

describe('the links of the first-launch screen', () => {
  it('are https and match the constants', () => {
    expect(PROJECT_URL.startsWith('https://')).toBe(true);
    for (const url of Object.values(PRIVACY_POLICY_URLS))
      expect(url.startsWith('https://')).toBe(true);
    expect(anchor('about')).toContain(`href="${PROJECT_URL}"`);
    expect(anchor('privacy')).toContain(`href="${PRIVACY_POLICY_URLS.en}"`);
  });

  it('open outside the shell', () => {
    for (const id of ['about', 'privacy']) {
      expect(anchor(id)).toContain('target="_blank"');
      expect(anchor(id)).toContain('rel="noopener noreferrer"');
    }
  });
});

describe('the privacy policy link', () => {
  it('is German for a German UI and English otherwise', () => {
    expect(privacyPolicyUrl('de')).toBe('https://legal.lktec.org/mowdeck/');
    expect(privacyPolicyUrl('de-AT')).toBe('https://legal.lktec.org/mowdeck/');
    expect(privacyPolicyUrl('en')).toBe('https://legal.lktec.org/en/mowdeck/');
    expect(privacyPolicyUrl('fr')).toBe('https://legal.lktec.org/en/mowdeck/');
    expect(privacyPolicyUrl(undefined)).toBe('https://legal.lktec.org/en/mowdeck/');
  });

  it('is on the About page, outside the shell, with the English URL as the static default', () => {
    const about = readFileSync(resolve(import.meta.dirname, '../about.html'), 'utf8');
    const match = /<a\s+id="privacy-policy"[^>]*>/.exec(about);
    expect(match).not.toBeNull();
    expect(match![0]).toContain(`href="${PRIVACY_POLICY_URLS.en}"`);
    expect(match![0]).toContain('target="_blank"');
    expect(match![0]).toContain('rel="noopener noreferrer"');
  });
});

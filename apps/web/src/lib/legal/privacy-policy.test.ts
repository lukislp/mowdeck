// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { describe, expect, it } from 'vitest';

import { PRIVACY_POLICY_URLS, privacyPolicyUrl } from './privacy-policy';

describe('privacyPolicyUrl', () => {
  it.each(['de', 'de-DE', 'DE-at'])('links the German policy for %s', (language) => {
    expect(privacyPolicyUrl(language)).toBe('https://legal.lktec.org/mowdeck/');
  });

  it.each(['en', 'en-GB', 'fr', '', undefined])('links the English policy for %s', (language) => {
    expect(privacyPolicyUrl(language)).toBe('https://legal.lktec.org/en/mowdeck/');
  });

  it('keeps both URLs on legal.lktec.org over https', () => {
    for (const url of Object.values(PRIVACY_POLICY_URLS)) {
      const parsed = new URL(url);
      expect(parsed.protocol).toBe('https:');
      expect(parsed.hostname).toBe('legal.lktec.org');
    }
  });
});

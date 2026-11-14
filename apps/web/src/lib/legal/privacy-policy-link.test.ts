// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';

import { PRIVACY_POLICY_URLS } from '#lib/legal/privacy-policy.js';
import { getLocale } from '#lib/paraglide/runtime.js';

import PrivacyPolicyLink from './PrivacyPolicyLink.svelte';

describe('PrivacyPolicyLink', () => {
  it('links the policy of the UI language in a new tab', () => {
    render(PrivacyPolicyLink);
    const link = screen.getByTestId('privacy-policy-link');
    const expected = getLocale().startsWith('de') ? PRIVACY_POLICY_URLS.de : PRIVACY_POLICY_URLS.en;
    expect(link.getAttribute('href')).toBe(expected);
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
  });
});

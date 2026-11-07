// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import { OFFICIAL_REPOSITORY } from '#lib/legal/source.js';

import ReportProblem from './ReportProblem.svelte';

describe('ReportProblem', () => {
  it('opens the pre-filled bug report form', async () => {
    const open = vi.fn();
    render(ReportProblem, {
      source: { repository: OFFICIAL_REPOSITORY, version: '0.60.3', ref: null },
      open,
    });
    await fireEvent.click(screen.getByTestId('report-problem'));
    expect(open).toHaveBeenCalledOnce();
    const url = new URL(open.mock.calls[0]?.[0] as string);
    expect(url.origin + url.pathname).toBe('https://github.com/lukislp/mowdeck/issues/new');
    expect(url.searchParams.get('template')).toBe('bug_report.yml');
    expect(url.searchParams.get('version')).toBe('0.60.3');
    expect(url.searchParams.get('environment')).toContain('Platform: web');
    expect(url.searchParams.get('environment')).toContain('Demo: no');
  });
});

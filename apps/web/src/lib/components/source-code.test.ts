import { render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { resetConfig } from '#lib/config.js';
import { DEFAULT_SOURCE, OFFICIAL_REPOSITORY } from '#lib/legal/source.js';

import SourceCode from './SourceCode.svelte';

const release = { repository: OFFICIAL_REPOSITORY, version: '0.3.0', ref: 'v0.3.0' };

describe('SourceCode', () => {
  afterEach(() => {
    resetConfig();
    vi.unstubAllGlobals();
  });

  it('links a release to the source of its tag in a new tab', () => {
    render(SourceCode, { props: { source: release } });
    const link = screen.getByTestId('source-code-link');
    expect(link.getAttribute('href')).toBe(`${OFFICIAL_REPOSITORY}/tree/v0.3.0`);
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toContain('noopener');
    expect(screen.getByTestId('source-version').textContent).toBe('v0.3.0');
    expect(screen.getByTestId('source-code').textContent).toContain('AGPL-3.0');
  });

  it('shows a development build and links to the repository', () => {
    render(SourceCode, { props: { source: DEFAULT_SOURCE } });
    expect(screen.getByTestId('source-version').textContent).toBe('dev');
    expect(screen.getByTestId('source-code-link').getAttribute('href')).toBe(OFFICIAL_REPOSITORY);
  });

  it('spells out the address of the build in the about sheet', () => {
    const fork = { repository: 'https://github.com/someone/fork', version: '1.0.0', ref: null };
    render(SourceCode, { props: { variant: 'sheet', source: fork } });
    const link = screen.getByTestId('source-code-link');
    expect(link.getAttribute('href')).toBe('https://github.com/someone/fork/tree/v1.0.0');
    expect(link.textContent).toContain('github.com/someone/fork/tree/v1.0.0');
    expect(screen.getByTestId('source-version').textContent?.trim()).toBe('v1.0.0');
  });

  it('reads the build from /config.json', async () => {
    const body = { source: { url: OFFICIAL_REPOSITORY, version: '0.4.1', ref: '' } };
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        Promise.resolve(
          new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } }),
        ),
      ),
    );
    render(SourceCode);
    await vi.waitFor(() => expect(screen.getByTestId('source-version').textContent).toBe('v0.4.1'));
    expect(screen.getByTestId('source-code-link').getAttribute('href')).toBe(
      `${OFFICIAL_REPOSITORY}/tree/v0.4.1`,
    );
  });
});

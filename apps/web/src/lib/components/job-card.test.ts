import { render, screen } from '@testing-library/svelte';
import { beforeEach, describe, expect, it } from 'vitest';

import type { JobView } from '$lib/mower/job';

import JobCard from './JobCard.svelte';

import { setLocale } from '$lib/paraglide/runtime';

beforeEach(() => setLocale('de', { reload: false }));

const running: JobView = {
  percent: 42,
  area: { kind: 'area', id: 'front', name: 'Front lawn' },
  elapsedS: 25 * 60,
  remainingS: 3600 + 5 * 60,
  mowedM2: 37.25,
  paused: false,
};

describe('JobCard', () => {
  it('shows progress, area, times and the mowed area', () => {
    render(JobCard, { props: { job: running } });
    expect(screen.getByTestId('job-percent').textContent).toContain('42');
    expect(screen.getByTestId('job-area').textContent).toContain('Front lawn');
    const bar = screen.getByTestId('job-progress');
    expect(bar.getAttribute('aria-valuenow')).toBe('42');
    expect(bar.querySelectorAll('.tick.on')).toHaveLength(9);
    expect(screen.getByTestId('job-elapsed').textContent).toContain('25 min');
    expect(screen.getByTestId('job-remaining').textContent).toContain('1 h 05');
    expect(screen.getByTestId('job-mowed').textContent).toContain('37,3');
    expect(screen.getByTestId('job-card').dataset.paused).toBe('false');
    expect(screen.queryByTestId('job-paused')).toBeNull();
  });

  it('says when the mower drives between areas and marks a paused job', () => {
    render(JobCard, {
      props: { job: { ...running, area: { kind: 'transit' }, paused: true, mowedM2: 240 } },
    });
    expect(screen.getByTestId('job-area').textContent).toContain('Unterwegs zwischen Bereichen');
    expect(screen.getByTestId('job-card').dataset.paused).toBe('true');
    expect(screen.getByTestId('job-paused').textContent).toContain('angehalten');
    expect(screen.getByTestId('job-mowed').textContent).toContain('240');
  });

  it('shows dashes for values the mower does not report', () => {
    render(JobCard, {
      props: {
        job: {
          percent: null,
          area: { kind: 'unknown' },
          elapsedS: null,
          remainingS: null,
          mowedM2: null,
          paused: false,
        },
      },
    });
    expect(screen.queryByTestId('job-progress')).toBeNull();
    expect(screen.getByTestId('job-progress-unknown')).toBeTruthy();
    expect(screen.getByTestId('job-area').textContent).toContain('Bereich nicht gemeldet');
    expect(screen.getByTestId('job-elapsed').textContent?.trim()).toBe('—');
    expect(screen.getByTestId('job-mowed').textContent?.trim()).toBe('—');
  });
});

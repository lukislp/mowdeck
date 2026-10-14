import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import MowerCard from '../lib/mower/MowerCard.svelte';
import Readout from '../lib/status/Readout.svelte';

describe('Readout', () => {
  it('shows value and unit', () => {
    render(Readout, { props: { label: 'Battery', value: '87', unit: '%', emptyText: 'no data' } });
    expect(screen.getByText('87')).toBeInTheDocument();
    expect(screen.getByText('%')).toBeInTheDocument();
  });

  it('shows the empty text instead of a missing value', () => {
    render(Readout, { props: { label: 'RTK', value: null, emptyText: 'no data' } });
    expect(screen.getByText('no data')).toBeInTheDocument();
  });

  it('marks a commanded value as pending', () => {
    const { container } = render(Readout, {
      props: { label: 'Blade', value: '50', unit: 'mm', emptyText: '-', pending: true },
    });
    expect(container.querySelector('.md-readout')).toHaveClass('pending');
  });
});

describe('MowerCard', () => {
  const base = {
    href: '/mowers/1',
    name: 'Luba',
    detail: 'simulated',
    rtkLabel: 'Fix',
    age: '5 s',
  } as const;

  it('is one link with name, activity, battery and RTK', () => {
    render(MowerCard, {
      props: { ...base, activity: 'mowing', activityLabel: 'Mowing', battery: '87', rtk: 'fixed' },
    });
    const link = screen.getByRole('link');
    expect(link).toHaveAttribute('href', '/mowers/1');
    expect(link).toHaveTextContent('Luba');
    expect(link).toHaveTextContent('Mowing');
    expect(link).toHaveTextContent('87');
  });

  it('shows the error line instead of the detail', () => {
    render(MowerCard, {
      props: {
        ...base,
        activity: 'error',
        activityLabel: 'Error',
        battery: null,
        rtk: 'none',
        error: 'E12 stuck',
      },
    });
    expect(screen.getByText('E12 stuck')).toBeInTheDocument();
    expect(screen.queryByText('simulated')).toBeNull();
  });
});

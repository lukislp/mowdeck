import { render } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import StatusBadge from '../lib/status/StatusBadge.svelte';
import { ACTIVITIES, activityTone } from '../lib/types.js';

describe('StatusBadge', () => {
  it.each(ACTIVITIES)('renders %s with its word, a glyph and a tone', (activity) => {
    const label = 'label-' + activity;
    const { container, getByText } = render(StatusBadge, { props: { activity, label } });
    expect(getByText(label)).toBeInTheDocument();
    const badge = container.querySelector('.md-badge');
    expect(badge?.getAttribute('data-tone')).toBe(activityTone(activity));
    expect(badge?.querySelector('svg')).not.toBeNull();
  });

  it('marks offline and stale states with a dashed outline', () => {
    const offline = render(StatusBadge, { props: { activity: 'offline', label: 'Offline' } });
    expect(offline.container.querySelector('.md-badge')).toHaveClass('dashed');
    const stale = render(StatusBadge, { props: { activity: 'mowing', label: 'M', stale: true } });
    expect(stale.container.querySelector('.md-badge')).toHaveClass('dashed');
  });
});

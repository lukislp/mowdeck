import { render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { describe, expect, it } from 'vitest';
import TableScroll from '../lib/layout/TableScroll.svelte';

const table = createRawSnippet(() => ({
  render: () => '<table><tbody><tr><td>wide</td></tr></tbody></table>',
}));

describe('TableScroll', () => {
  it('wraps the table in a named scroll region', () => {
    render(TableScroll, { props: { children: table, label: 'Per mower', testid: 'rows' } });
    const region = screen.getByRole('region', { name: 'Per mower' });
    expect(region).toHaveClass('md-table-scroll');
    expect(region).toHaveAttribute('data-testid', 'rows');
    expect(region.querySelector('table')).not.toBeNull();
  });

  it('is a plain box without a label', () => {
    const { container } = render(TableScroll, { props: { children: table } });
    const box = container.querySelector('.md-table-scroll');
    expect(box).not.toBeNull();
    expect(box).not.toHaveAttribute('role');
    expect(screen.queryByRole('region')).toBeNull();
  });
});

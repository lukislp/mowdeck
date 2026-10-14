import { render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { describe, expect, it } from 'vitest';
import Can from '../lib/permission/Can.svelte';

const text = (value: string) =>
  createRawSnippet(() => ({ render: () => '<span>' + value + '</span>' }));

describe('Can', () => {
  it('renders the children when allowed', () => {
    render(Can, { props: { allowed: true, children: text('start') } });
    expect(screen.getByText('start')).toBeInTheDocument();
  });

  it('renders the fallback when not allowed', () => {
    render(Can, { props: { allowed: false, children: text('start'), fallback: text('nope') } });
    expect(screen.queryByText('start')).toBeNull();
    expect(screen.getByText('nope')).toBeInTheDocument();
  });

  it('renders nothing without a fallback', () => {
    const { container } = render(Can, { props: { allowed: false, children: text('start') } });
    expect(container.textContent).toBe('');
  });
});

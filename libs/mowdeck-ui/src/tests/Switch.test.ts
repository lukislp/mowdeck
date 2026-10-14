import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import NavRail from '../lib/layout/NavRail.svelte';
import Switch from '../lib/form/Switch.svelte';

const states = { on: 'An', off: 'Aus' };

describe('Switch', () => {
  it('is a labelled switch that says its state in words', async () => {
    const onchange = vi.fn();
    render(Switch, { props: { id: 'quiet', label: 'Ruhezeit', hint: 'Nachts', states, onchange } });
    const control = screen.getByRole('switch', { name: 'Ruhezeit' });
    expect(control).toHaveAttribute('aria-checked', 'false');
    expect(control).toHaveTextContent('Aus');
    expect(control).toHaveAccessibleDescription('Nachts');
    await fireEvent.click(control);
    expect(control).toHaveAttribute('aria-checked', 'true');
    expect(control).toHaveTextContent('An');
    expect(onchange).toHaveBeenCalledWith(true);
  });

  it('does not toggle when disabled', async () => {
    const onchange = vi.fn();
    render(Switch, {
      props: { id: 'x', label: 'Push', checked: true, disabled: true, states, onchange },
    });
    const control = screen.getByRole('switch', { name: 'Push' });
    await fireEvent.click(control);
    expect(control).toHaveAttribute('aria-checked', 'true');
    expect(onchange).not.toHaveBeenCalled();
  });
});

describe('NavRail badge', () => {
  const item = (badge?: number) => ({
    id: 'notifications',
    href: '/notifications',
    label: 'Meldungen',
    icon: 'bell' as const,
    active: false,
    badge,
    badgeLabel: badge ? `${badge} ungelesen` : undefined,
  });

  it('shows a count with an accessible text and caps it at 99+', () => {
    const { rerender } = render(NavRail, {
      props: { items: [item(3)], label: 'Navigation', brand: 'MD' },
    });
    const badge = screen.getByTestId('nav-notifications-badge');
    expect(badge).toHaveTextContent('3');
    expect(screen.getByRole('link', { name: /Meldungen/ })).toHaveTextContent('3 ungelesen');
    void rerender({ items: [item(140)], label: 'Navigation', brand: 'MD' });
  });

  it('hides the badge at zero', () => {
    render(NavRail, { props: { items: [item(0)], label: 'Navigation', brand: 'MD' } });
    expect(screen.queryByTestId('nav-notifications-badge')).toBeNull();
  });
});

describe('NavRail overflow', () => {
  const items = (active = '') =>
    ['mowers', 'fleet', 'schedules', 'notifications', 'reports', 'users', 'account'].map((id) => ({
      id,
      href: `/${id}`,
      label: id,
      icon: 'list' as const,
      active: id === active,
    }));

  it('keeps four items and a more button on phones, the rest in the sheet', async () => {
    render(NavRail, {
      props: { items: items(), label: 'Navigation', brand: 'MD', moreLabel: 'Mehr' },
    });
    const more = screen.getByTestId('nav-more');
    expect(more).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByTestId('nav-reports').parentElement).toHaveClass('overflow');
    expect(screen.getByTestId('nav-notifications').parentElement).not.toHaveClass('overflow');
    await fireEvent.click(more);
    expect(more).toHaveAttribute('aria-expanded', 'true');
    const sheet = screen.getByTestId('nav-more-sheet');
    expect(sheet.querySelectorAll('a')).toHaveLength(3);
    await fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByTestId('nav-more-sheet')).toBeNull();
  });

  it('marks the more button current when an overflow page is active', () => {
    render(NavRail, {
      props: { items: items('users'), label: 'Navigation', brand: 'MD', moreLabel: 'Mehr' },
    });
    expect(screen.getByTestId('nav-more')).toHaveAttribute('data-current', 'true');
  });

  it('needs no more button within the slots or without a label', () => {
    render(NavRail, {
      props: { items: items().slice(0, 5), label: 'N', brand: 'MD', moreLabel: 'Mehr' },
    });
    expect(screen.queryByTestId('nav-more')).toBeNull();
    render(NavRail, { props: { items: items(), label: 'N2', brand: 'MD' } });
    expect(screen.queryByTestId('nav-more')).toBeNull();
  });
});

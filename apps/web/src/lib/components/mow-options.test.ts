import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';

import { offeredPatterns } from '$lib/mower/mow-pattern';
import { setLocale } from '$lib/paraglide/runtime';
import { emptyForm } from '$lib/schedules/form';
import { emptyMow, type CapsLike } from '$lib/schedules/mow-options';

import MowOptions from './MowOptions.svelte';
import MowSettings from './MowSettings.svelte';
import ScheduleForm from './ScheduleForm.svelte';

const ALL = offeredPatterns(['stripes', 'diagonal', 'checkerboard', 'edge_only', 'random_angle']);

describe('MowOptions (shared by the start sheet and the schedule editor)', () => {
  it('is the start sheet body: same controls, values always set', () => {
    setLocale('de', { reload: false });
    render(MowSettings, {
      props: {
        offered: ['stripes', 'diagonal'],
        spacing: null,
        draft: { name: 'stripes', angle: 0, laps: 1, spacingMm: null },
        remember: true,
      },
    });
    expect(screen.getByTestId('start-pattern').querySelectorAll('[role="radio"]')).toHaveLength(2);
    // No "Mäher" state for pattern, angle and laps in the start sheet.
    expect(screen.getByTestId('start-angle').textContent).not.toContain('Mäher');
    expect(screen.getByTestId('start-border-laps').textContent).not.toContain('Mäher');
  });

  it('leaves values empty in a schedule and resets them to the mower', async () => {
    setLocale('de', { reload: false });
    const draft = emptyMow();
    render(MowOptions, {
      props: {
        offered: ALL,
        spacing: [200, 350],
        draft: draft.pattern,
        unset: true,
        prefix: 'sched-mow',
        bladeRange: [25, 70],
        speedRange: null,
      },
    });
    const radios = () => [
      ...screen.getByTestId('sched-mow-pattern').querySelectorAll('[role="radio"]'),
    ];
    expect(radios().every((r) => r.getAttribute('aria-checked') === 'false')).toBe(true);
    expect(screen.getByTestId('sched-mow-angle').textContent).toContain('Mäher');
    expect(screen.getByTestId('sched-mow-border-laps').textContent).toContain('Mäher');
    expect(screen.getByTestId('sched-mow-blade').textContent).toContain('Mäher');

    await fireEvent.click(screen.getByTestId('sched-mow-pattern-diagonal'));
    expect(screen.getByTestId('sched-mow-pattern-diagonal').getAttribute('aria-checked')).toBe(
      'true',
    );
    // A second tap gives the pattern back to the mower.
    await fireEvent.click(screen.getByTestId('sched-mow-pattern-diagonal'));
    expect(screen.getByTestId('sched-mow-pattern-diagonal').getAttribute('aria-checked')).toBe(
      'false',
    );

    // From "Mäher" the first step up goes to 15°, then reset.
    await fireEvent.click(screen.getByLabelText('Winkel vergrößern'));
    const angle = screen.getByTestId('sched-mow-angle');
    expect(angle.querySelector('output')?.dataset.value).toBe('15');
    await fireEvent.click(angle.querySelector('.reset') as HTMLElement);
    expect(angle.querySelector('output')?.dataset.value).toBe('');

    await fireEvent.click(screen.getByLabelText('Mehr Randrunden'));
    expect(screen.getByTestId('sched-mow-border-laps').querySelector('output')?.dataset.value).toBe(
      '1',
    );

    await fireEvent.click(screen.getByLabelText('Schnitthöhe erhöhen'));
    const blade = screen.getByTestId('sched-mow-blade');
    expect(blade.querySelector('output')?.dataset.value).toBe('30');
    await fireEvent.click(blade.querySelector('.reset') as HTMLElement);
    expect(blade.textContent).toContain('Mäher');
  });

  it('shows nothing the mower does not declare', () => {
    render(MowOptions, {
      props: {
        offered: ['stripes'],
        spacing: null,
        draft: emptyMow().pattern,
        unset: true,
        prefix: 'sched-mow',
        options: null,
      },
    });
    // No start_options: no "Mehr" and none of the job options; no ranges: no steppers.
    expect(screen.queryByTestId('sched-mow-more')).toBeNull();
    expect(screen.queryByTestId('sched-mow-obstacle')).toBeNull();
    expect(screen.queryByTestId('sched-mow-spacing')).toBeNull();
    expect(screen.queryByTestId('sched-mow-setpoints')).toBeNull();
    expect(screen.queryByText(/nicht unterstützt|not supported/i)).toBeNull();
    expect(screen.getByTestId('sched-mow-pattern').querySelectorAll('[role="radio"]')).toHaveLength(
      1,
    );
  });

  it('never offers the start progress when the caller leaves it out', async () => {
    render(MowOptions, {
      props: {
        offered: ['stripes'],
        spacing: null,
        draft: emptyMow().pattern,
        unset: true,
        prefix: 'sched-mow',
        options: { detection: ['standard'], obstacleLaps: null, orders: [], progress: null },
      },
    });
    await fireEvent.click(screen.getByTestId('sched-mow-more-toggle'));
    expect(screen.getByTestId('sched-mow-obstacle')).toBeInTheDocument();
    expect(screen.queryByTestId('sched-mow-progress')).toBeNull();
  });
});

describe('ScheduleForm start settings', () => {
  const GROUP = 'g1';
  const props = (capabilities: Record<string, CapsLike> | null) => ({
    form: emptyForm('2026-10-24', { type: 'group', id: GROUP }),
    mowers: [
      { id: 'm1', name: 'Luba' },
      { id: 'm2', name: 'Yuka' },
    ],
    groups: [{ id: GROUP, name: 'Garten', related: ['m1', 'm2'] }],
    mowerNames: { m1: 'Luba', m2: 'Yuka' },
    capabilities,
    submitLabel: 'Speichern',
    onsubmit: () => {},
  });

  it('offers a group only the patterns every member runs and lists the members', () => {
    setLocale('de', { reload: false });
    render(ScheduleForm, {
      props: props({
        m1: {
          model: 'LUBA 2 AWD',
          native_patterns: ['stripes', 'diagonal', 'checkerboard'],
          start_options: { obstacle_detection: ['off', 'standard', 'sensitive'] },
        },
        m2: { model: 'Yuka', native_patterns: ['stripes', 'checkerboard', 'edge_only'] },
      }),
    });
    const tiles = screen.getByTestId('sched-mow-pattern').querySelectorAll('[role="radio"]');
    expect([...tiles].map((t) => t.getAttribute('data-value'))).toEqual([
      'stripes',
      'checkerboard',
    ]);
    // Only the AWD offers obstacle detection: the group gets no "Mehr".
    expect(screen.queryByTestId('sched-mow-more')).toBeNull();
    const members = screen.getByTestId('sched-mow-members');
    expect(members.textContent).toContain('Gilt für 2 Mäher der Gruppe');
    expect(members.textContent).toContain('Luba');
    expect(members.textContent).toContain('LUBA 2 AWD');
    expect(members.textContent).toContain('Yuka');
    expect(screen.getByTestId('sched-mow-summary')).toHaveTextContent('Standard des Mähers');
  });

  it('offers nothing while a member has unknown capabilities', () => {
    setLocale('de', { reload: false });
    render(ScheduleForm, {
      props: props({ m1: { model: 'LUBA 2 AWD', native_patterns: ['stripes'] } }),
    });
    expect(screen.queryByTestId('sched-mow-pattern')).toBeNull();
    expect(screen.getByTestId('sched-mow-none')).toBeInTheDocument();
    expect(screen.getByTestId('sched-mow-members').textContent).toContain('Fähigkeiten unbekannt');
  });
});

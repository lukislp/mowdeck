// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';

import { safety } from './gate.svelte';
import SafetyCheck from './SafetyCheck.svelte';
import SafetySheet from './SafetySheet.svelte';

afterEach(() => safety.cancel());

describe('SafetyCheck', () => {
  it('states the rule and offers an unticked box inside its label', () => {
    render(SafetyCheck, { props: { checked: false } });
    const box = screen.getByTestId('safety-check-box') as HTMLInputElement;
    const text = screen.getByTestId('safety-check').textContent ?? '';
    expect(box.checked).toBe(false);
    expect(text).toMatch(/people, children and animals/);
    expect(text).toMatch(/manual/);
    expect(screen.getByTestId('safety-check').dataset.variant).toBe('now');
    // The label is the touch target (`min-height: var(--md-target)`).
    expect(box.closest('label')).not.toBeNull();
  });

  it('adds the unattended sentence and its own confirmation for a schedule', () => {
    render(SafetyCheck, { props: { checked: false, variant: 'unattended' } });
    const text = screen.getByTestId('safety-check').textContent ?? '';
    expect(text).toMatch(/without you/);
    expect(text).toMatch(/every scheduled start/);
  });
});

describe('SafetySheet', () => {
  it('stays closed until a feature asks', () => {
    render(SafetySheet);
    expect(screen.queryByTestId('safety-sheet')).toBeNull();
  });

  it('keeps Confirm disabled until the box is ticked, then resolves true', async () => {
    render(SafetySheet);
    const answer = safety.ask('start_mow');
    const confirm = (await screen.findByTestId('safety-confirm')) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    await fireEvent.click(screen.getByTestId('safety-check-box'));
    expect(confirm.disabled).toBe(false);
    await fireEvent.click(confirm);
    expect(await answer).toBe(true);
    expect(screen.queryByTestId('safety-sheet')).toBeNull();
  });

  it('puts the keyboard on the box when it opens', async () => {
    render(SafetySheet);
    void safety.ask('drive_session');
    const box = await screen.findByTestId('safety-check-box');
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.activeElement).toBe(box);
  });

  it('cancels with the button and with Escape, and asks unticked the next time', async () => {
    render(SafetySheet);
    const first = safety.ask('start_mow');
    await fireEvent.click(await screen.findByTestId('safety-check-box'));
    await fireEvent.click(screen.getByTestId('safety-cancel'));
    expect(await first).toBe(false);

    const second = safety.ask('start_mow');
    const box = (await screen.findByTestId('safety-check-box')) as HTMLInputElement;
    expect(box.checked).toBe(false);
    await fireEvent.keyDown(window, { key: 'Escape' });
    expect(await second).toBe(false);
  });
});

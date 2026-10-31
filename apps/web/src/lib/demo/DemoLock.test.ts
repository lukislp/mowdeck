import { render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';

import { ApiProblem, problemFromBody } from '#lib/api/problem.js';
import { problemMessage } from '#lib/i18n.js';
import { setLocale } from '#lib/paraglide/runtime.js';

import DemoLock from './DemoLock.svelte';
import { demo } from './state.svelte.js';

const form = createRawSnippet(() => ({
  render: () => '<form><input aria-label="Name" /><button type="submit">Speichern</button></form>',
}));

describe('DemoLock', () => {
  afterEach(() => {
    demo.config = null;
    setLocale('de', { reload: false });
  });

  it('changes nothing outside the demo', () => {
    render(DemoLock, { props: { children: form } });
    expect(screen.queryByTestId('demo-lock')).toBeNull();
    expect(screen.getByRole('button', { name: 'Speichern' })).toBeEnabled();
    expect(screen.getByLabelText('Name')).toBeEnabled();
  });

  it('locks the form in the demo with one plain hint', () => {
    setLocale('de', { reload: false });
    demo.config = { resetHours: 6 };
    render(DemoLock, { props: { children: form, testid: 'demo-lock-x' } });
    const lock = screen.getByTestId('demo-lock-x');
    expect(lock).toHaveTextContent('In der Demo nicht verfügbar');
    expect(screen.getByRole('button', { name: 'Speichern' })).toBeDisabled();
    expect(screen.getByLabelText('Name')).toBeDisabled();
    // The fieldset names the hint, so assistive tech hears why the controls are off.
    const fieldset = lock.querySelector('fieldset');
    const note = document.getElementById(fieldset?.getAttribute('aria-describedby') ?? '');
    expect(note).toHaveTextContent('In der Demo nicht verfügbar');
  });

  it('speaks English in the English app', () => {
    setLocale('en', { reload: false });
    demo.config = { resetHours: 6 };
    render(DemoLock, { props: { children: form, variant: 'inline' } });
    expect(screen.getByTestId('demo-lock')).toHaveTextContent('Not available in the demo');
    expect(screen.getByRole('button', { name: 'Speichern' })).toBeDisabled();
  });

  it('hides entry points in the demo', () => {
    demo.config = { resetHours: 6 };
    const { container } = render(DemoLock, { props: { children: form, variant: 'hide' } });
    expect(container.querySelector('form')).toBeNull();
  });
});

describe('demo refusal (403 demo-mode)', () => {
  const refusal = () =>
    problemFromBody(403, {
      type: 'urn:mowdeck:problem:demo-mode',
      title: 'Not available in the demo',
      status: 403,
      detail: 'The demo sends nothing outside the app: notifications stay in the inbox.',
    });

  afterEach(() => setLocale('de', { reload: false }));

  it('is one translated sentence, without the service English reason', () => {
    setLocale('de', { reload: false });
    const problem = refusal();
    expect(problem.detail).toBeNull();
    expect(problem.message).not.toMatch(/sends nothing/);
    expect(problemMessage(problem)).toMatch(/^In der Demo nicht verfügbar/);
    setLocale('en', { reload: false });
    expect(problemMessage(refusal())).toMatch(/^Not available in the demo/);
  });

  it('keeps the detail of every other problem', () => {
    const other = new ApiProblem({
      type: 'urn:mowdeck:problem:conflict',
      status: 409,
      detail: 'name taken',
    });
    expect(other.detail).toBe('name taken');
  });
});

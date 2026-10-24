import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';

import { initialState, type KindSchema, type OptionSchema } from '#lib/connection/form.js';
import { m } from '#lib/paraglide/messages.js';

import ConnectionForm from './ConnectionForm.svelte';

function option(key: string, extra: Partial<OptionSchema> = {}): OptionSchema {
  return {
    key,
    type: 'string',
    title: key,
    description: '',
    required: false,
    secret: false,
    managed: false,
    group: null,
    advanced: false,
    pattern: null,
    format_hint: null,
    minimum: null,
    maximum: null,
    default: null,
    examples: [],
    ...extra,
  };
}

const CLOUD: KindSchema = {
  kind: 'cloud',
  title: 'Mammotion cloud',
  description: '',
  extra_options: false,
  linked_account: true,
  groups: [],
  options: [
    option('device_name', { managed: true }),
    option('iot_id', { managed: true }),
    option('refresh_seconds', { type: 'number', advanced: true, minimum: 10, maximum: 300 }),
  ],
};

describe('ConnectionForm', () => {
  it('shows managed options as read-only readouts, not inputs', () => {
    const state = initialState(CLOUD, { device_name: 'Luba-FAKE0001', iot_id: 'fakeiot0001' });
    render(ConnectionForm, { props: { schema: CLOUD, state } });
    expect(screen.getByTestId('managed-device_name')).toHaveTextContent('Luba-FAKE0001');
    expect(screen.getByTestId('managed-iot_id')).toHaveTextContent('fakeiot0001');
    expect(document.getElementById('opt-device_name')).toBeNull();
    expect(document.getElementById('opt-iot_id')).toBeNull();
    // The advanced extras stay editable.
    expect(document.getElementById('opt-refresh_seconds')).toBeInstanceOf(HTMLInputElement);
    expect(screen.getByText(m.linked_managed_hint())).toBeInTheDocument();
  });

  it('marks managed values Fleet has not set yet', () => {
    render(ConnectionForm, { props: { schema: CLOUD, state: initialState(CLOUD, null) } });
    expect(screen.getByTestId('managed-iot_id')).toHaveTextContent(m.linked_managed_empty());
  });
});

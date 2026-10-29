import { describe, expect, it } from 'vitest';

import { ApiProblem } from '#lib/api/problem.js';

import {
  buildOptions,
  initialState,
  isActive,
  isManaged,
  issueFor,
  serverIssues,
  validate,
  withProviderOptions,
  SECRET_MASK,
  type KindSchema,
  type OptionSchema,
} from './form';

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

const MAC = '^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$';

const BLE: KindSchema = {
  kind: 'ble_bridge',
  title: 'BLE bridge',
  description: '',
  extra_options: false,
  linked_account: false,
  groups: [{ key: 'credential', title: 'Key' }],
  options: [
    option('host', { required: true }),
    option('port', { type: 'integer', minimum: 1, maximum: 65535, default: 6053 }),
    option('noise_psk', { secret: true, group: 'credential' }),
    option('credential_ref', { group: 'credential', pattern: '^(env|file):\\S+$' }),
    option('ble_address', { required: true, pattern: MAC }),
    option('allow_manual_blades', { type: 'boolean', default: false }),
    option('scan_timeout_s', { type: 'number', minimum: 1, maximum: 600, advanced: true }),
  ],
};

/** A kind with alternatives (the cloud schema before linked accounts). */
const GROUPED: KindSchema = {
  kind: 'cloud',
  title: 'Cloud',
  description: '',
  extra_options: false,
  linked_account: false,
  groups: [
    { key: 'device', title: 'Mower' },
    { key: 'credential', title: 'Credential' },
  ],
  options: [
    option('account', { required: true }),
    option('password', { secret: true, group: 'credential' }),
    option('credential_ref', { group: 'credential' }),
    option('device_name', { group: 'device' }),
    option('iot_id', { group: 'device' }),
  ],
};

/** The cloud kind of linked accounts: Fleet sets the device; only advanced extras are editable. */
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
    option('refresh_seconds', {
      type: 'number',
      advanced: true,
      minimum: 10,
      maximum: 300,
      default: 20,
    }),
  ],
};

const SIMULATED: KindSchema = {
  kind: 'simulated',
  title: 'Simulated',
  description: '',
  extra_options: true,
  linked_account: false,
  groups: [],
  options: [],
};

describe('connection forms', () => {
  it('requires the account, one device id and one credential with alternatives', () => {
    const state = initialState(GROUPED, null);
    expect(state.choice).toEqual({ credential: 'password', device: 'device_name' });
    expect(Object.keys(validate(GROUPED, state)).sort()).toEqual([
      'account',
      'device_name',
      'password',
    ]);
    state.values.account = 'second@example.com';
    state.choice.device = 'iot_id';
    state.values.iot_id = 'iot-1';
    state.choice.credential = 'credential_ref';
    state.values.credential_ref = 'env:MAMMOTION_PASSWORD';
    expect(validate(GROUPED, state)).toEqual({});
    // Only the chosen alternatives are sent.
    state.values.password = 'left over';
    expect(buildOptions(GROUPED, state, null)).toEqual({
      account: 'second@example.com',
      iot_id: 'iot-1',
      credential_ref: 'env:MAMMOTION_PASSWORD',
    });
  });

  it('checks the BLE address, the port range and number types', () => {
    const state = initialState(BLE, null);
    state.values.host = 'bridge.local';
    state.values.noise_psk = 'c2VjcmV0';
    state.values.ble_address = 'AA:BB:CC';
    state.values.port = '70000';
    state.values.scan_timeout_s = 'abc';
    expect(validate(BLE, state)).toEqual({
      port: { code: 'range' },
      ble_address: { code: 'pattern' },
      scan_timeout_s: { code: 'number' },
    });
    state.values.port = '60.5';
    expect(validate(BLE, state).port).toEqual({ code: 'integer' });
    state.values.port = '6053';
    state.values.ble_address = 'aa:bb:cc:dd:ee:ff';
    state.values.scan_timeout_s = '2,5';
    state.flags.allow_manual_blades = true;
    expect(validate(BLE, state)).toEqual({});
    expect(buildOptions(BLE, state, null)).toEqual({
      host: 'bridge.local',
      port: 6053,
      noise_psk: 'c2VjcmV0',
      ble_address: 'aa:bb:cc:dd:ee:ff',
      allow_manual_blades: true,
      scan_timeout_s: 2.5,
    });
  });

  it('never prefills a secret, marks it set and sends the mask back to keep it', () => {
    const stored = {
      host: 'bridge.local',
      noise_psk: SECRET_MASK,
      ble_address: 'AA:BB:CC:DD:EE:FF',
      allow_manual_blades: false,
    };
    const state = initialState(BLE, stored);
    expect(state.values.noise_psk).toBe('');
    expect(state.secretSet.noise_psk).toBe(true);
    expect(state.choice.credential).toBe('noise_psk');
    expect(validate(BLE, state)).toEqual({});
    expect(buildOptions(BLE, state, stored).noise_psk).toBe(SECRET_MASK);
    state.values.noise_psk = 'bmV3';
    expect(buildOptions(BLE, state, stored).noise_psk).toBe('bmV3');
    // A reference chosen before is the active alternative.
    const byRef = initialState(BLE, { ...stored, noise_psk: undefined, credential_ref: 'env:PSK' });
    expect(byRef.choice.credential).toBe('credential_ref');
    expect(isActive(BLE.options[2]!, byRef)).toBe(false);
  });

  it('shows managed options but never validates or sends them', () => {
    const stored = { device_name: 'Luba-FAKE0001', iot_id: 'fakeiot0001', refresh_seconds: 30 };
    const state = initialState(CLOUD, stored);
    expect(isManaged(CLOUD.options[0]!)).toBe(true);
    expect(isManaged(CLOUD.options[2]!)).toBe(false);
    expect(state.values.device_name).toBe('Luba-FAKE0001');
    expect(state.values.iot_id).toBe('fakeiot0001');
    expect(buildOptions(CLOUD, state, stored)).toEqual({ refresh_seconds: 30 });
    // Even a changed value of a managed key is not sent, and an empty one is no error.
    state.values.device_name = 'Luba-OTHER';
    state.values.iot_id = '';
    expect(validate(CLOUD, state)).toEqual({});
    expect(buildOptions(CLOUD, state, stored)).toEqual({ refresh_seconds: 30 });
    state.values.refresh_seconds = '5';
    expect(validate(CLOUD, state)).toEqual({ refresh_seconds: { code: 'range' } });
    expect(buildOptions(CLOUD, initialState(CLOUD, null), null)).toEqual({});
  });

  it('keeps options the schema does not describe', () => {
    const state = initialState(SIMULATED, { time_scale: 30 });
    expect(buildOptions(SIMULATED, state, { time_scale: 30 })).toEqual({ time_scale: 30 });
    expect(buildOptions(SIMULATED, state, null)).toEqual({});
    expect(validate(SIMULATED, state)).toEqual({});
  });

  it('maps the 422 field errors of Fleet to the options', () => {
    const problem = new ApiProblem({
      status: 422,
      errors: [
        { pointer: '#/connection_options/noise_psk', message: 'one of ...', code: 'one_of' },
        { pointer: '#/connection_options/ble_address', message: 'bad', code: 'pattern' },
        { pointer: '#/connection_options/unknown', message: 'odd', code: 'whatever' },
        { pointer: '#/name', message: 'too long' },
      ],
    });
    const issues = serverIssues(BLE, problem);
    expect(issues).toEqual({
      noise_psk: { code: 'required' },
      ble_address: { code: 'pattern' },
      unknown: { code: 'server', message: 'odd' },
    });
    // The group's error lands on whichever alternative is shown.
    expect(issueFor(BLE, BLE.options[3]!, issues)).toEqual({ code: 'required' });
    expect(issueFor(BLE, BLE.options[0]!, issues)).toBeNull();
    expect(serverIssues(BLE, null)).toEqual({});
  });
});

describe('withProviderOptions', () => {
  const refresh = option('night_mode', { type: 'boolean' });
  const withProviders: KindSchema = {
    ...CLOUD,
    provider_options: { acme: [refresh, option(CLOUD.options[0]!.key)], quiet: [] },
  };

  it('adds the options the mower provider declares, never twice', () => {
    const merged = withProviderOptions(withProviders, { cloud_provider: 'acme' });
    expect(merged.options.map((o) => o.key)).toEqual([
      ...CLOUD.options.map((o) => o.key),
      'night_mode',
    ]);
  });

  it('adds nothing for a provider without settings, another kind or an unknown provider', () => {
    expect(withProviderOptions(withProviders, { cloud_provider: 'quiet' })).toBe(withProviders);
    expect(withProviderOptions(withProviders, { cloud_provider: 'gone' })).toBe(withProviders);
    expect(withProviderOptions(withProviders, {})).toBe(withProviders);
    expect(withProviderOptions(BLE, { cloud_provider: 'acme' })).toBe(BLE);
  });
});

import { describe, expect, it } from 'vitest';

import { ApiProblem, PROBLEM_PREFIX } from '#lib/api/problem.js';
import { m } from '#lib/paraglide/messages.js';

import fleetSpec from '../../../openapi/fleet.json';
import {
  accountIssue,
  addableNames,
  BULK_LIMIT,
  bulkBody,
  bulkOutcome,
  deviceState,
  emailLooksValid,
  failureMessage,
  idPart,
  inUseMowers,
  isLegacyCloud,
  onlineState,
  pruneSelection,
  sortDevices,
  syncDrafts,
  toggleSelection,
  type LinkedDevice,
} from './accounts';

function device(name: string, extra: Partial<LinkedDevice> = {}): LinkedDevice {
  return {
    device_name: name,
    model: null,
    iot_id: null,
    product_key: null,
    online: null,
    is_mower: true,
    device_type: 'mower',
    registered: false,
    mower_id: null,
    registered_elsewhere: false,
    ...extra,
  };
}

// The fake Mammotion cloud of the e2e stack, plus one mower of this and one of another account.
const DEVICES: LinkedDevice[] = [
  device('RTK-FAKE0001', { is_mower: false, device_type: 'rtk_base', online: true }),
  device('Luba-FAKE0002', { online: false, iot_id: 'fakeiot0002' }),
  device('Luba-ADDED', { registered: true, mower_id: 'm-1' }),
  device('Luba-FOREIGN', { registered_elsewhere: true }),
  device('Luba-FAKE0001', { online: true, iot_id: 'fakeiot0001', model: 'LUBA 2 AWD 5000X' }),
];

describe('linked account devices', () => {
  it('tells addable mowers from added, foreign and non-mowers', () => {
    expect(DEVICES.map(deviceState)).toEqual([
      'not_mower',
      'addable',
      'registered',
      'elsewhere',
      'addable',
    ]);
    // A base station is never a mower, whatever else it says.
    expect(deviceState(device('RTK', { is_mower: false, registered: true }))).toBe('not_mower');
    expect(addableNames(DEVICES)).toEqual(['Luba-FAKE0002', 'Luba-FAKE0001']);
  });

  it('reads the online flag with an unknown state', () => {
    expect(DEVICES.map(onlineState)).toEqual(['online', 'offline', 'unknown', 'unknown', 'online']);
  });

  it('sorts the addable mowers first, by name', () => {
    expect(sortDevices(DEVICES).map((d) => d.device_name)).toEqual([
      'Luba-FAKE0001',
      'Luba-FAKE0002',
      'Luba-ADDED',
      'Luba-FOREIGN',
      'RTK-FAKE0001',
    ]);
    expect(DEVICES[0]!.device_name).toBe('RTK-FAKE0001'); // not sorted in place
  });

  it('toggles and prunes the selection', () => {
    let selected = toggleSelection([], 'Luba-FAKE0001', true);
    selected = toggleSelection(selected, 'Luba-FAKE0002', true);
    selected = toggleSelection(selected, 'Luba-FAKE0001', true);
    expect(selected).toEqual(['Luba-FAKE0002', 'Luba-FAKE0001']);
    expect(toggleSelection(selected, 'Luba-FAKE0002', false)).toEqual(['Luba-FAKE0001']);
    // After a re-read only addable devices stay selected.
    expect(
      pruneSelection(['Luba-FAKE0001', 'Luba-ADDED', 'RTK-FAKE0001', 'Gone'], DEVICES),
    ).toEqual(['Luba-FAKE0001']);
  });
});

describe('bulk registration', () => {
  it('keeps drafts of the selection and starts new ones with the device name', () => {
    const first = syncDrafts(['Luba-FAKE0001'], {}, 'site-default');
    expect(first).toEqual({ 'Luba-FAKE0001': { name: 'Luba-FAKE0001', siteId: 'site-default' } });
    const renamed = { 'Luba-FAKE0001': { name: 'Front', siteId: 'site-2' } };
    expect(syncDrafts(['Luba-FAKE0001', 'Luba-FAKE0002'], renamed, null)).toEqual({
      'Luba-FAKE0001': { name: 'Front', siteId: 'site-2' },
      'Luba-FAKE0002': { name: 'Luba-FAKE0002', siteId: null },
    });
    expect(syncDrafts([], renamed, null)).toEqual({});
  });

  it('builds the body in selection order with trimmed names and sites', () => {
    const body = bulkBody(['Luba-FAKE0002', 'Luba-FAKE0001', 'Luba-X'], {
      'Luba-FAKE0001': { name: '  Front lawn ', siteId: 'site-2' },
      'Luba-FAKE0002': { name: '   ', siteId: null },
    });
    expect(body).toEqual({
      mowers: [
        { device_name: 'Luba-FAKE0002' },
        { device_name: 'Luba-FAKE0001', name: 'Front lawn', site_id: 'site-2' },
        { device_name: 'Luba-X' },
      ],
    });
    expect(BULK_LIMIT).toBe(fleetSpec.components.schemas.LinkedMowersIn.properties.mowers.maxItems);
  });

  it('keeps the failed devices selected with their message', () => {
    const outcome = bulkOutcome({
      created: [],
      failed: [
        {
          device_name: 'Luba-FAKE0002',
          status: 409,
          type: `${PROBLEM_PREFIX}mower-already-registered`,
          detail: 'already registered',
        },
        { device_name: 'Luba-X', status: 404, type: `${PROBLEM_PREFIX}not-found`, detail: 'gone' },
      ],
    });
    expect(outcome.keep).toEqual(['Luba-FAKE0002', 'Luba-X']);
    expect(outcome.failures).toEqual({
      'Luba-FAKE0002': m.linked_failure_registered(),
      'Luba-X': 'gone',
    });
    expect(
      failureMessage({ device_name: 'a', status: 500, type: 'about:blank', detail: 'boom' }),
    ).toBe('boom');
  });
});

describe('account problems', () => {
  it('puts a refused login on the password field', () => {
    const issue = accountIssue(
      new ApiProblem({
        type: `${PROBLEM_PREFIX}linked-account-login-failed`,
        status: 422,
        errors: [{ pointer: '#/password', message: 'refused', code: 'login_failed' }],
      }),
    );
    expect(issue).toEqual({ field: 'password', message: m.linked_login_failed(), detail: null });
  });

  it('maps already linked, a bad e-mail, an unreachable cloud and the rest', () => {
    expect(
      accountIssue(new ApiProblem({ type: `${PROBLEM_PREFIX}conflict`, status: 409 })),
    ).toEqual({ field: 'email', message: m.linked_already_linked(), detail: null });
    expect(
      accountIssue(
        new ApiProblem({
          status: 422,
          type: `${PROBLEM_PREFIX}validation-failed`,
          errors: [{ pointer: '#/email', message: 'not an email' }],
        }),
      ).field,
    ).toBe('email');
    expect(
      accountIssue(
        new ApiProblem({ status: 503, detail: 'no encryption key configured', type: 'x' }),
      ),
    ).toEqual({
      field: null,
      message: m.linked_unavailable(),
      detail: 'no encryption key configured',
    });
    const other = accountIssue(ApiProblem.network());
    expect(other.field).toBeNull();
    expect(other.message).toBe(m.problem_network());
  });

  it('lists the mowers that still use an account', () => {
    const problem = new ApiProblem({
      type: `${PROBLEM_PREFIX}linked-account-in-use`,
      status: 409,
      errors: [
        { pointer: '#/mowers/m-1', message: 'Front', code: 'in_use' },
        { pointer: '#/mowers/m-2', message: 'Back', code: 'in_use' },
        { pointer: '#/other', message: 'ignored' },
      ],
    });
    expect(inUseMowers(problem)).toEqual([
      { id: 'm-1', name: 'Front' },
      { id: 'm-2', name: 'Back' },
    ]);
    expect(inUseMowers(new ApiProblem({ status: 409 }))).toEqual([]);
  });
});

describe('small helpers', () => {
  it('makes element ids, checks e-mails and spots legacy cloud mowers', () => {
    expect(idPart('Luba-FAKE0001')).toBe('Luba-FAKE0001');
    expect(idPart('Luba VP 2/ä')).toBe('Luba_VP_2__');
    expect(emailLooksValid(' conn-1@fake.mowdeck.test ')).toBe(true);
    expect(emailLooksValid('nope')).toBe(false);
    expect(emailLooksValid('a b@c')).toBe(false);
    expect(isLegacyCloud({ connection_kind: 'cloud' })).toBe(true);
    expect(
      isLegacyCloud({
        connection_kind: 'cloud',
        linked_account_id: 'la-1',
      }),
    ).toBe(false);
    expect(isLegacyCloud({ connection_kind: 'simulated' })).toBe(false);
  });
});

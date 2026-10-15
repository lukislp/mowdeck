import { describe, expect, it, vi } from 'vitest';

import { ApiProblem } from '$lib/api/problem';
import type { BridgeLink, BridgeScanReply, ScannedDevice } from '$lib/fleet/links';
import { m } from '$lib/paraglide/messages';

import { formIssues, parsePort, preselect, rssiText, sortDevices } from './bridge-link';
import { BridgeLinkFlow, type BridgeLinkDeps } from './bridge-link.svelte';

const LUBA: ScannedDevice = { name: 'Luba-VP1234', address: 'FC:23:00:00:00:01', rssi: -62 };
const OTHER: ScannedDevice = { name: 'Luba-VP9999', address: 'FC:23:00:00:00:09', rssi: -40 };
const PROXY = {
  proxy_name: 'mowdeck-bridge',
  esphome_version: '2026.3.0',
  compiled_at: 'Mar 1 2026',
  project_name: 'mowdeck.bridge',
  project_version: '1',
  checked_at: '2026-10-14T10:00:00Z',
  outdated: false,
};

function reply(partial: Partial<BridgeScanReply>): BridgeScanReply {
  return { proxy: PROXY, devices: [], error: null, ...partial };
}

function deps(overrides: Partial<BridgeLinkDeps> = {}): BridgeLinkDeps {
  return {
    scan: vi.fn(async () => reply({ devices: [OTHER, LUBA] })),
    save: vi.fn(async (input): Promise<BridgeLink> => ({
      host: input.host,
      port: input.port,
      ble_address: input.ble_address,
      device_name: input.device_name,
      allow_manual_blades: input.allow_manual_blades,
      manual_blade_height_mm: null,
      proxy_info: PROXY,
    })),
    expectedName: () => 'Luba-VP1234',
    existing: () => null,
    ...overrides,
  };
}

function filled(flow: BridgeLinkFlow): void {
  flow.start();
  flow.host = '192.0.2.10';
  flow.key = 'c2VjcmV0';
}

describe('bridge form checks', () => {
  it('parses the port with the default for an empty field', () => {
    expect(parsePort('')).toBe(6053);
    expect(parsePort(' 6054 ')).toBe(6054);
    expect(parsePort('0')).toBeNull();
    expect(parsePort('70000')).toBeNull();
    expect(parsePort('abc')).toBeNull();
  });

  it('requires host and key, and a port in range', () => {
    expect(formIssues({ host: '', port: '', key: '' })).toEqual({
      host: 'required',
      key: 'required',
    });
    expect(formIssues({ host: 'b', port: '99999', key: 'k' })).toEqual({ port: 'range' });
    // A saved link keeps its key when the field stays empty.
    expect(formIssues({ host: 'b', port: '', key: '' }, true)).toEqual({});
  });
});

describe('scanned mowers', () => {
  it('puts the expected mower first, then the strongest signal, without duplicates', () => {
    const weak = { ...OTHER, address: 'FC:23:00:00:00:02', rssi: -80 };
    const list = sortDevices([weak, OTHER, LUBA, { ...LUBA }], 'luba-vp1234');
    expect(list.map((d) => d.address)).toEqual([LUBA.address, OTHER.address, weak.address]);
  });

  it('preselects the expected mower, a single other one, else nothing', () => {
    expect(preselect([OTHER, LUBA], 'Luba-VP1234')).toBe(LUBA.address);
    expect(preselect([OTHER], 'Luba-VP1234')).toBe(OTHER.address);
    expect(
      preselect([OTHER, { ...OTHER, address: 'FC:23:00:00:00:02' }], 'Luba-VP1234'),
    ).toBeNull();
    expect(preselect([], null)).toBeNull();
  });

  it('writes the signal with a real minus sign', () => {
    expect(rssiText(-62)).toBe('−62 dBm');
    expect(rssiText(null)).toBeNull();
  });
});

describe('bridge link flow', () => {
  it('walks form, scanning, pick and saved, sending what was chosen', async () => {
    const d = deps();
    const flow = new BridgeLinkFlow(d);
    filled(flow);
    expect(flow.open).toBe(true);
    expect(flow.step).toBe('form');
    const scanning = flow.scan();
    expect(flow.step).toBe('scanning');
    await scanning;
    expect(d.scan).toHaveBeenCalledWith(
      { host: '192.0.2.10', port: 6053, noise_psk: 'c2VjcmV0' },
      expect.any(AbortSignal),
    );
    expect(flow.step).toBe('pick');
    expect(flow.error).toBeNull();
    // The expected mower is preselected and listed first; the other one is "anderer Mäher".
    expect(flow.chosen).toBe(LUBA.address);
    expect(flow.sorted.map((x) => x.name)).toEqual(['Luba-VP1234', 'Luba-VP9999']);
    flow.allowBlades = true;
    const saving = flow.save();
    expect(flow.step).toBe('saving');
    await saving;
    expect(d.save).toHaveBeenCalledWith({
      host: '192.0.2.10',
      port: 6053,
      noise_psk: 'c2VjcmV0',
      ble_address: LUBA.address,
      device_name: 'Luba-VP1234',
      allow_manual_blades: true,
    });
    expect(flow.step).toBe('saved');
    expect(flow.saved?.device_name).toBe('Luba-VP1234');
    // The key never lingers after the save.
    expect(flow.key).toBe('');
  });

  it('does not scan with an incomplete form and shows the issues after the first try', async () => {
    const d = deps();
    const flow = new BridgeLinkFlow(d);
    flow.start();
    expect(flow.issues).toEqual({});
    await flow.scan();
    expect(d.scan).not.toHaveBeenCalled();
    expect(flow.step).toBe('form');
    expect(flow.issues).toEqual({ host: 'required', key: 'required' });
    flow.host = 'bridge.local';
    flow.key = 'k';
    flow.port = '0';
    expect(flow.issues).toEqual({ port: 'range' });
  });

  it.each([
    ['unreachable', m.bridge_error_unreachable({ host: '192.0.2.10', port: '6054' })],
    ['auth_failed', m.bridge_error_auth_failed()],
    ['timeout', m.bridge_error_timeout()],
    ['busy', m.bridge_error_busy()],
    ['unsupported', m.bridge_error_unsupported()],
  ] as const)('maps the scan error %s to its text and stays on the form', async (code, text) => {
    const d = deps({
      scan: vi.fn(async () => reply({ devices: [], error: { code, detail: 'x' } })),
    });
    const flow = new BridgeLinkFlow(d);
    filled(flow);
    flow.port = '6054';
    await flow.scan();
    expect(flow.step).toBe('form');
    expect(flow.errorCode).toBe(code);
    expect(flow.error).toBe(text);
  });

  it('offers the other mowers it saw when the expected one timed out', async () => {
    const d = deps({
      scan: vi.fn(async () =>
        reply({ devices: [OTHER], error: { code: 'timeout', detail: 'not seen' } }),
      ),
    });
    const flow = new BridgeLinkFlow(d);
    filled(flow);
    await flow.scan();
    expect(flow.step).toBe('pick');
    expect(flow.error).toBe(m.bridge_error_timeout());
    expect(flow.chosen).toBe(OTHER.address);
    flow.back();
    expect(flow.step).toBe('form');
    expect(flow.error).toBeNull();
  });

  it('turns a 503 into the "no gateway answered" text and other problems into their message', async () => {
    const d = deps({
      scan: vi.fn(async () => {
        throw new ApiProblem({ status: 503 });
      }),
    });
    const flow = new BridgeLinkFlow(d);
    filled(flow);
    await flow.scan();
    expect(flow.step).toBe('form');
    expect(flow.errorCode).toBe('request');
    expect(flow.error).toBe(m.bridge_error_gateway());

    const forbidden = new BridgeLinkFlow(
      deps({
        scan: vi.fn(async () => {
          throw new ApiProblem({ status: 403, type: 'urn:mowdeck:problem:forbidden' });
        }),
      }),
    );
    filled(forbidden);
    await forbidden.scan();
    expect(forbidden.error).toBe(m.problem_forbidden());
  });

  it('ignores the answer of a scan that was abandoned by closing the sheet', async () => {
    let resolve: (value: BridgeScanReply) => void = () => undefined;
    let signal: AbortSignal | null = null;
    const d = deps({
      scan: vi.fn(
        (_input, s) =>
          new Promise<BridgeScanReply>((r) => {
            signal = s;
            resolve = r;
          }),
      ),
    });
    const flow = new BridgeLinkFlow(d);
    filled(flow);
    const scanning = flow.scan();
    flow.close();
    expect(signal!.aborted).toBe(true);
    resolve(reply({ devices: [LUBA] }));
    await scanning;
    expect(flow.open).toBe(false);
    expect(flow.devices).toEqual([]);
    expect(flow.step).toBe('scanning');
    // Opening again starts clean.
    flow.start();
    expect(flow.step).toBe('form');
    expect(flow.key).toBe('');
  });

  it('prefills a stored link and keeps its key with ***', async () => {
    const stored: BridgeLink = {
      host: 'bridge.local',
      port: 6060,
      ble_address: LUBA.address,
      device_name: 'Luba-VP1234',
      allow_manual_blades: true,
      manual_blade_height_mm: null,
      proxy_info: null,
    };
    const d = deps({ existing: () => stored, scan: vi.fn(async () => reply({ devices: [LUBA] })) });
    const flow = new BridgeLinkFlow(d);
    flow.start();
    expect(flow.host).toBe('bridge.local');
    expect(flow.port).toBe('6060');
    expect(flow.advanced).toBe(true);
    expect(flow.allowBlades).toBe(true);
    expect(flow.keyStored).toBe(true);
    await flow.scan();
    expect(d.scan).toHaveBeenCalledWith(
      { host: 'bridge.local', port: 6060, noise_psk: '***' },
      expect.any(AbortSignal),
    );
    await flow.save();
    expect(d.save).toHaveBeenCalledWith(expect.objectContaining({ noise_psk: '***' }));
    expect(flow.step).toBe('saved');
  });

  it('keeps the list when the save fails and says why', async () => {
    const d = deps({
      save: vi.fn(async () => {
        throw new ApiProblem({ status: 409, type: 'urn:mowdeck:problem:conflict' });
      }),
    });
    const flow = new BridgeLinkFlow(d);
    filled(flow);
    await flow.scan();
    await flow.save();
    expect(flow.step).toBe('pick');
    expect(flow.error).toBe(m.problem_conflict());
    expect(flow.chosen).toBe(LUBA.address);
  });
});

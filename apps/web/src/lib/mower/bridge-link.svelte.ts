/**
 * The Bluetooth bridge sheet's flow, without any markup (design 3): step 1 host, port and key,
 * step 2 the scan through the proxy (one at a time, cancelled when the sheet closes) and the
 * choice among the mowers it saw, step 3 the saved link. The page constructs it with the real
 * Fleet calls and binds to its state; `bridge-link.test.ts` drives it with fakes.
 */
import { asProblem } from '$lib/api/problem';
import type {
  BridgeLink,
  BridgeLinkIn,
  BridgeScanErrorCode,
  BridgeScanIn,
  BridgeScanReply,
  ProxyInfo,
  ScannedDevice,
} from '$lib/fleet/links';

import {
  DEFAULT_PORT,
  formIssues,
  parsePort,
  preselect,
  scanErrorText,
  scanProblemText,
  sortDevices,
  type BridgeFormField,
  type BridgeFormIssue,
} from './bridge-link';

export type BridgeStep = 'form' | 'scanning' | 'pick' | 'saving' | 'saved';

export interface BridgeLinkDeps {
  /** `POST /mowers/{id}/links/ble_bridge/scan`. */
  scan(input: BridgeScanIn, signal: AbortSignal): Promise<BridgeScanReply>;
  /** `PUT /mowers/{id}/links/ble_bridge`. */
  save(input: BridgeLinkIn): Promise<BridgeLink>;
  /** The mower's Bluetooth name as its registration knows it, or null. */
  expectedName(): string | null;
  /** The stored link when the sheet edits one (host and port prefilled, key kept), else null. */
  existing(): BridgeLink | null;
}

const KEEP_KEY = '***';

export class BridgeLinkFlow {
  open = $state(false);
  step = $state<BridgeStep>('form');
  host = $state('');
  port = $state(String(DEFAULT_PORT));
  key = $state('');
  /** The port row is shown ("erweitert"). */
  advanced = $state(false);
  /** Client checks run live after the first "Mäher suchen". */
  tried = $state(false);
  devices = $state<ScannedDevice[]>([]);
  proxy = $state<ProxyInfo | null>(null);
  /** The chosen mower's address. */
  chosen = $state<string | null>(null);
  allowBlades = $state(false);
  /** The text of the last failure, shown on the step it belongs to. */
  error = $state<string | null>(null);
  errorCode = $state<BridgeScanErrorCode | 'request' | null>(null);
  saved = $state<BridgeLink | null>(null);
  #abort: AbortController | null = null;
  #editing = false;

  constructor(private readonly deps: BridgeLinkDeps) {}

  /** A saved link keeps its key when the field stays empty. */
  get keyStored(): boolean {
    return this.#editing;
  }

  issues = $derived.by((): Partial<Record<BridgeFormField, BridgeFormIssue>> => {
    if (!this.tried) return {};
    return formIssues({ host: this.host, port: this.port, key: this.key }, this.keyStored);
  });
  expectedName = $derived.by(() => this.deps.expectedName());
  /** The scanned mowers, the expected one first. */
  sorted = $derived(sortDevices(this.devices, this.expectedName));
  chosenDevice = $derived(this.sorted.find((d) => d.address === this.chosen) ?? null);
  /** The port that goes out (the default while the field is empty). */
  portNumber = $derived(parsePort(this.port) ?? DEFAULT_PORT);

  /** Open the sheet; with a stored link its host and port are prefilled and the key is kept. */
  start(): void {
    const existing = this.deps.existing();
    this.#editing = existing !== null;
    this.host = existing?.host ?? '';
    this.port = String(existing?.port ?? DEFAULT_PORT);
    this.key = '';
    this.advanced = existing !== null && existing.port !== DEFAULT_PORT;
    this.allowBlades = existing?.allow_manual_blades ?? false;
    this.tried = false;
    this.devices = [];
    this.proxy = null;
    this.chosen = null;
    this.error = null;
    this.errorCode = null;
    this.saved = null;
    this.step = 'form';
    this.open = true;
  }

  /** Close the sheet; a running scan is abandoned and its answer ignored. */
  close(): void {
    this.#abort?.abort();
    this.#abort = null;
    this.open = false;
    this.key = '';
  }

  /** Back to the form from the mower list (the values stay). */
  back(): void {
    if (this.step === 'pick') {
      this.step = 'form';
      this.error = null;
      this.errorCode = null;
    }
  }

  #fail(text: string, code: BridgeScanErrorCode | 'request', step: BridgeStep): void {
    this.error = text;
    this.errorCode = code;
    this.step = step;
  }

  /** "Mäher suchen": validate, then scan through the proxy (up to 30 s). */
  async scan(): Promise<void> {
    if (this.step === 'scanning' || this.step === 'saving') return;
    this.tried = true;
    if (Object.keys(this.issues).length > 0) return;
    const host = this.host.trim();
    const port = this.portNumber;
    const key = this.key.trim();
    const input: BridgeScanIn = { host, port, noise_psk: key === '' ? KEEP_KEY : key };
    this.#abort?.abort();
    const abort = new AbortController();
    this.#abort = abort;
    this.error = null;
    this.errorCode = null;
    this.step = 'scanning';
    let reply: BridgeScanReply;
    try {
      reply = await this.deps.scan(input, abort.signal);
    } catch (error) {
      if (abort.signal.aborted) return;
      this.#fail(scanProblemText(asProblem(error)), 'request', 'form');
      return;
    }
    if (abort.signal.aborted) return;
    this.proxy = reply.proxy ?? null;
    this.devices = reply.devices ?? [];
    this.chosen = preselect(this.devices, this.expectedName);
    if (reply.error && this.devices.length === 0) {
      // Nothing to choose from: back to the form with the reason (the proxy may have answered).
      this.#fail(scanErrorText(reply.error, host, port), reply.error.code, 'form');
      return;
    }
    if (reply.error) {
      // The expected mower was not seen but others were: offer them, say why it is odd.
      this.#fail(scanErrorText(reply.error, host, port), reply.error.code, 'pick');
      return;
    }
    this.step = 'pick';
  }

  choose(address: string): void {
    if (this.step === 'pick') this.chosen = address;
  }

  /** "Speichern": the chosen mower, the bridge and the blade choice go to Fleet. */
  async save(): Promise<void> {
    const device = this.chosenDevice;
    if (this.step !== 'pick' || !device) return;
    const key = this.key.trim();
    const input: BridgeLinkIn = {
      host: this.host.trim(),
      port: this.portNumber,
      noise_psk: key === '' ? KEEP_KEY : key,
      ble_address: device.address,
      device_name: device.name,
      allow_manual_blades: this.allowBlades,
    };
    this.error = null;
    this.errorCode = null;
    this.step = 'saving';
    try {
      this.saved = await this.deps.save(input);
      this.key = '';
      this.step = 'saved';
    } catch (error) {
      this.#fail(scanProblemText(asProblem(error)), 'request', 'pick');
    }
  }
}

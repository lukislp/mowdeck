/**
 * Setup credentials of one Matter node: the setup passcode and the discriminator, and the two
 * onboarding codes a user types or scans (Matter Core § 5.1).
 *
 * Every mower gets its own random passcode (27 bit, 1..99999998) and discriminator (12 bit). The
 * specification forbids trivial passcodes (§ 5.1.7.1): 00000000, 11111111, ... 99999999,
 * 12345678 and 87654321. They are never generated: a draw that hits one is drawn again, and
 * matter.js' own check (`isValidPasscode`) must agree before a passcode is used.
 *
 * Passcodes and codes are secrets. Nothing in this module logs them, and no caller may.
 */
import { randomInt } from 'node:crypto';

import {
  DiscoveryCapabilitiesSchema,
  ManualPairingCodeCodec,
  QrPairingCodeCodec,
  isValidPasscode,
} from '@matter/main/types';
import { encode } from 'uqr';

/** Inclusive bounds of a setup passcode (Matter Core § 5.1.1.6). */
export const PASSCODE_MIN = 1;
export const PASSCODE_MAX = 99_999_998;
/** Passcodes the specification forbids (Matter Core § 5.1.7.1). */
export const INVALID_PASSCODES: readonly number[] = [
  0, 11_111_111, 22_222_222, 33_333_333, 44_444_444, 55_555_555, 66_666_666, 77_777_777,
  88_888_888, 99_999_999, 12_345_678, 87_654_321,
];
/** Largest 12-bit discriminator. */
export const DISCRIMINATOR_MAX = 0xfff;

/** Matter's test vendor and a product id from its test range: MowDeck is not certified, so the
 * controllers show "uncertified accessory" before they add it. */
export const VENDOR_ID = 0xfff1;
export const PRODUCT_ID = 0x8000;

export interface SetupCredentials {
  passcode: number;
  discriminator: number;
}

export interface PairingCodes {
  /** The 11-digit manual pairing code as Apple Home shows it, `1234-567-8901`. */
  manual: string;
  /** The QR code payload, `MT:...`. */
  qrPayload: string;
}

/** True when `passcode` may be used: in range and not one of the forbidden values. */
export function passcodeAllowed(passcode: number): boolean {
  return (
    Number.isInteger(passcode) &&
    passcode >= PASSCODE_MIN &&
    passcode <= PASSCODE_MAX &&
    !INVALID_PASSCODES.includes(passcode) &&
    isValidPasscode(passcode)
  );
}

/** A random source of integers in `[min, max)`; `crypto.randomInt` unless a test hands one in. */
export type RandomInt = (min: number, max: number) => number;

/** New random setup credentials; never a forbidden passcode. */
export function generateCredentials(random: RandomInt = randomInt): SetupCredentials {
  for (let attempt = 0; attempt < 64; attempt++) {
    const passcode = random(PASSCODE_MIN, PASSCODE_MAX + 1);
    if (passcodeAllowed(passcode)) {
      return { passcode, discriminator: random(0, DISCRIMINATOR_MAX + 1) };
    }
  }
  // Twelve forbidden values out of almost 10^8: only a broken random source ends up here.
  throw new Error('the random source keeps returning forbidden passcodes');
}

/** True when stored credentials are still usable (a hand-edited file could break them). */
export function credentialsValid(value: SetupCredentials): boolean {
  return (
    passcodeAllowed(value.passcode) &&
    Number.isInteger(value.discriminator) &&
    value.discriminator >= 0 &&
    value.discriminator <= DISCRIMINATOR_MAX
  );
}

/** `12345678901` as `1234-567-8901` (11 digits; anything else is returned unchanged). */
export function formatManualCode(code: string): string {
  const digits = code.replace(/\D/g, '');
  if (digits.length !== 11) return code;
  return `${digits.slice(0, 4)}-${digits.slice(4, 7)}-${digits.slice(7)}`;
}

/** The onboarding codes of a node with these credentials (standard commissioning flow, on the
 * IP network). */
export function pairingCodes(credentials: SetupCredentials): PairingCodes {
  const manual = ManualPairingCodeCodec.encode({
    discriminator: credentials.discriminator,
    passcode: credentials.passcode,
  });
  const qrPayload = QrPairingCodeCodec.encode([
    {
      version: 0,
      vendorId: VENDOR_ID,
      productId: PRODUCT_ID,
      flowType: 0,
      discoveryCapabilities: DiscoveryCapabilitiesSchema.encode({ onIpNetwork: true }),
      discriminator: credentials.discriminator,
      passcode: credentials.passcode,
    },
  ]);
  return { manual: formatManualCode(manual), qrPayload };
}

/** The QR code of `payload` as rows of `0` (light) and `1` (dark) modules, without the quiet
 * zone; the web app draws it, so no image and no QR library ship to the browser. */
export function qrMatrix(payload: string): string[] {
  const { data } = encode(payload, { ecc: 'M', border: 0 });
  return data.map((row) => row.map((dark) => (dark ? '1' : '0')).join(''));
}

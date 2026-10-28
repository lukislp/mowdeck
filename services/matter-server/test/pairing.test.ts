import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { ManualPairingCodeCodec, QrPairingCodeCodec } from '@matter/main/types';

import {
  DISCRIMINATOR_MAX,
  INVALID_PASSCODES,
  PASSCODE_MAX,
  credentialsValid,
  formatManualCode,
  generateCredentials,
  pairingCodes,
  passcodeAllowed,
  qrMatrix,
} from '../src/pairing.ts';

describe('passcodes', () => {
  it('rejects every passcode the specification forbids, and the range ends', () => {
    for (const code of INVALID_PASSCODES) assert.equal(passcodeAllowed(code), false, `${code}`);
    for (const code of [-1, 0, PASSCODE_MAX + 1, 99_999_999, 1.5, Number.NaN]) {
      assert.equal(passcodeAllowed(code), false, `${code}`);
    }
    assert.equal(passcodeAllowed(1), true);
    assert.equal(passcodeAllowed(PASSCODE_MAX), true);
    assert.equal(passcodeAllowed(20_202_021), true);
  });

  it('never generates a forbidden passcode, even from a source that keeps hitting them', () => {
    const queue = [11_111_111, 12_345_678, 87_654_321, 88_888_888, 34_567_890, 0x123];
    const random = (min: number, max: number) => {
      const next = queue.shift();
      assert.ok(next !== undefined);
      assert.ok(next >= min && next < max, `${next} outside [${min}, ${max})`);
      return next;
    };
    assert.deepEqual(generateCredentials(random), { passcode: 34_567_890, discriminator: 0x123 });
  });

  it('gives up on a broken random source instead of returning a forbidden passcode', () => {
    assert.throws(() => generateCredentials(() => 11_111_111), /forbidden/);
  });

  it('generates valid, varying credentials', () => {
    const seen = new Set<number>();
    for (let i = 0; i < 500; i++) {
      const credentials = generateCredentials();
      assert.ok(credentialsValid(credentials));
      assert.ok(credentials.discriminator >= 0 && credentials.discriminator <= DISCRIMINATOR_MAX);
      seen.add(credentials.passcode);
    }
    assert.ok(seen.size > 490);
  });

  it('checks stored credentials', () => {
    assert.equal(credentialsValid({ passcode: 20_202_021, discriminator: 3840 }), true);
    assert.equal(credentialsValid({ passcode: 20_202_021, discriminator: 4096 }), false);
    assert.equal(credentialsValid({ passcode: 12_345_678, discriminator: 1 }), false);
  });
});

describe('pairing codes', () => {
  it('formats the manual code like Apple Home', () => {
    assert.equal(formatManualCode('34970112332'), '3497-011-2332');
    assert.equal(formatManualCode('3497-011-2332'), '3497-011-2332');
    assert.equal(formatManualCode('123'), '123');
  });

  it('encodes codes that decode to the same passcode and discriminator', () => {
    const credentials = { passcode: 20_202_021, discriminator: 3840 };
    const codes = pairingCodes(credentials);
    assert.match(codes.manual, /^\d{4}-\d{3}-\d{4}$/);
    assert.equal(codes.manual, '3497-011-2332'); // matter.js' and CHIP's well-known test code
    const manual = ManualPairingCodeCodec.decode(codes.manual.replaceAll('-', ''));
    assert.equal(manual.passcode, credentials.passcode);
    assert.equal(manual.shortDiscriminator, credentials.discriminator >> 8);
    assert.match(codes.qrPayload, /^MT:/);
    const [qr] = QrPairingCodeCodec.decode(codes.qrPayload);
    assert.equal(qr?.passcode, credentials.passcode);
    assert.equal(qr?.discriminator, credentials.discriminator);
    assert.equal(qr?.vendorId, 0xfff1);
  });

  it('draws the QR code as square rows of modules', () => {
    const rows = qrMatrix(pairingCodes({ passcode: 20_202_021, discriminator: 3840 }).qrPayload);
    assert.ok(rows.length >= 21);
    for (const row of rows) assert.match(row, new RegExp(`^[01]{${rows.length}}$`));
    // Finder pattern in the top left corner.
    assert.equal(rows[0]?.slice(0, 7), '1111111');
  });
});

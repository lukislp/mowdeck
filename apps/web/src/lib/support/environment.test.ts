// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { describe, expect, it } from 'vitest';

import { firstMower } from './environment';

const MOWER = { id: 'm1', model: 'Luba VP', connection_kind: 'mammotion' };

describe('firstMower', () => {
  it('takes the model the capabilities report, not the one registered in Fleet', () => {
    const caps = [{ mower_id: 'm1', capabilities: { model: 'LUBA 2 AWD 3000X' } }];
    expect(firstMower([MOWER], caps)).toEqual({
      model: 'LUBA 2 AWD 3000X',
      connection_kind: 'mammotion',
    });
  });

  it("falls back to Fleet's model without capabilities for that mower", () => {
    const other = [{ mower_id: 'm2', capabilities: { model: 'YUKA' } }];
    expect(firstMower([MOWER], other)?.model).toBe('Luba VP');
    expect(firstMower([MOWER], [{ mower_id: 'm1', capabilities: null }])?.model).toBe('Luba VP');
  });

  it('is null without a mower', () => {
    expect(firstMower([], [])).toBeNull();
  });
});

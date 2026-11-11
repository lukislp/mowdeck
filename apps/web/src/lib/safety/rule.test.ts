// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { describe, expect, it } from 'vitest';

import { needsSafetyAcknowledgement, safetyVariant, type SafetyKind } from './rule';

describe('needsSafetyAcknowledgement', () => {
  it.each<SafetyKind>([
    'start_mow',
    'resume_mow',
    'spot_mow',
    'leave_dock',
    'drive_calibrate',
    'drive_session',
    'schedule_enable',
    'schedule_run_now',
  ])('asks before %s', (kind) => {
    expect(needsSafetyAcknowledgement(kind)).toBe(true);
  });

  it.each<SafetyKind>(['pause_mow', 'return_to_dock', 'emergency_stop', 'spot_stop'])(
    'never asks before %s (stopping is never delayed)',
    (kind) => {
      expect(needsSafetyAcknowledgement(kind)).toBe(false);
    },
  );

  it.each<SafetyKind>([
    'set_blade_height',
    'set_speed',
    'set_light',
    'set_drive_speed',
    'set_setting',
    'firmware_update',
    'restart_mower',
    'sync_map',
    'relocate_dock',
    'resync_rtk',
  ])('does not ask for %s, which does not move the mower', (kind) => {
    expect(needsSafetyAcknowledgement(kind)).toBe(false);
  });

  it('does not ask again when the flow already confirmed the same thing', () => {
    expect(needsSafetyAcknowledgement('spot_mow', true)).toBe(false);
  });
});

describe('safetyVariant', () => {
  it('words a schedule as an unattended start and everything else as a start now', () => {
    expect(safetyVariant('schedule_enable')).toBe('unattended');
    expect(safetyVariant('start_mow')).toBe('now');
    expect(safetyVariant('drive_session')).toBe('now');
    expect(safetyVariant('schedule_run_now')).toBe('now');
  });
});

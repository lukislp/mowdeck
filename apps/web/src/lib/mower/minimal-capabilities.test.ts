/**
 * A minimal adapter (ADR 0066): a vendor that only connects, reports its state and starts,
 * pauses and docks the mower. MowDeck shows what a mower can do and nothing more, so every
 * capability-driven part of the UI must come out empty for it: no camera, no light switch, no
 * blade or speed control, no device settings, no maintenance actions, no mowing patterns.
 */
import { describe, expect, it } from 'vitest';

import { hasCamera } from '#lib/drive/layout.js';
import { offerOf } from '#lib/schedules/mow-options.js';

import { settingSpecs } from './device-settings';
import { lightCapable } from './light';
import { bladeRange, speedRange } from './limits';
import { dockChoiceOffered } from './dock-choice';
import { offeredActions } from './maintenance';
import { startPlans } from './start-plans';

/** The capabilities a minimal cloud adapter reports: the contract defaults. */
const MINIMAL = {
  native_patterns: [],
  manual_drive: false,
  manual_drive_blades: false,
  custom_paths: false,
  camera: 'none',
  light: false,
  rtk_sources: [],
  blade_height_range_mm: null,
  max_speed_mps: null,
  connection_kinds: ['cloud'],
} as const;

describe('a mower with minimal capabilities', () => {
  it('shows no control the mower does not offer', () => {
    expect(hasCamera(MINIMAL)).toBe(false);
    expect(lightCapable(MINIMAL)).toBe(false);
    expect(bladeRange(MINIMAL)).toBeNull();
    expect(speedRange(MINIMAL)).toBeNull();
    expect(settingSpecs(MINIMAL)).toEqual([]);
    expect(offeredActions(MINIMAL)).toEqual([]);
    expect(startPlans(MINIMAL)).toEqual([]);
    expect(dockChoiceOffered(MINIMAL, true)).toBe(false); // a plain return, no keep-job choice
  });

  it('offers no mowing patterns, spacing or job options to start with', () => {
    const offer = offerOf(MINIMAL);
    expect(offer.offered).toEqual([]);
    expect(offer.spacing).toBeNull();
    expect(offer.options).toBeNull();
    expect(offer.blade).toBeNull();
    expect(offer.speed).toBeNull();
  });

  it('handles a mower that reports no capabilities yet the same way', () => {
    expect(hasCamera(null)).toBe(false);
    expect(settingSpecs(undefined)).toEqual([]);
    expect(offeredActions(null)).toEqual([]);
    expect(offerOf(null).offered).toEqual([]);
  });
});

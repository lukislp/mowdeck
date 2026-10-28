import { describe, expect, it } from 'vitest';

import { m } from '#lib/paraglide/messages.js';

import {
  ALARM_KINDS,
  USER_KINDS,
  alarmPosition,
  assistProposal,
  isAlarm,
  kindHint,
  kindLabel,
  kindTone,
} from './kinds';

describe('alarm kinds', () => {
  it('knows the theft alarms, all in the error tone', () => {
    expect([...ALARM_KINDS].sort()).toEqual([
      'geofence_exit',
      'mower_left_dock_unexpectedly',
      'mower_lifted',
    ]);
    for (const kind of ALARM_KINDS) expect(kindTone(kind)).toBe('error');
    expect(isAlarm('mower_stuck')).toBe(false);
    expect(isAlarm('geofence_exit')).toBe(true);
  });

  it('reads the reported position from the alarm link', () => {
    expect(alarmPosition('/mowers/m1/map?lat=48.137154&lon=11.576124')).toEqual({
      lat: '48.137154',
      lon: '11.576124',
    });
    expect(alarmPosition('https://deck.example/mowers/m1/map?lat=-33.9&lon=18.4')).toEqual({
      lat: '-33.900000',
      lon: '18.400000',
    });
    expect(alarmPosition('/mowers/m1/map')).toBeNull();
    expect(alarmPosition('/mowers/m1/map?lat=91&lon=0')).toBeNull();
    expect(alarmPosition('/mowers/m1/map?lat=abc&lon=1')).toBeNull();
    expect(alarmPosition(null)).toBeNull();
    expect(alarmPosition('http://[bad')).toBeNull();
  });
});

describe('vision proposal kind', () => {
  it('is a switchable warning kind with a label and a hint', () => {
    expect(USER_KINDS).toContain('vision_proposal');
    expect(kindLabel('vision_proposal')).toBe(m.notif_kind_vision_proposal());
    expect(kindHint('vision_proposal')).toBe(m.notif_kind_vision_proposal_hint());
    expect(kindTone('vision_proposal')).toBe('paused');
    expect(isAlarm('vision_proposal')).toBe(false);
  });

  it('has an automatic variant that links to the proposal the same way', () => {
    expect(USER_KINDS).toContain('vision_auto_proposal');
    expect(kindLabel('vision_auto_proposal')).toBe(m.notif_kind_vision_auto_proposal());
    expect(kindHint('vision_auto_proposal')).toBe(m.notif_kind_vision_auto_proposal_hint());
    expect(kindTone('vision_auto_proposal')).toBe('paused');
    expect(isAlarm('vision_auto_proposal')).toBe(false);
  });

  it('reads the proposal id from the deep link', () => {
    const id = '0b5f2c1e-7d4a-4b8e-9f3c-2a1d0e9c8b7a';
    expect(assistProposal(`/mowers/m1?assist=${id}`)).toBe(id);
    expect(assistProposal(`?assist=${id}`)).toBe(id);
    expect(assistProposal('/mowers/m1')).toBeNull();
    expect(assistProposal('/mowers/m1?assist=<script>')).toBeNull();
    expect(assistProposal(null)).toBeNull();
  });
});

describe('tracking kinds', () => {
  it('are switchable, labelled in both languages and not acknowledged alarms', () => {
    for (const kind of [
      'tracking_started',
      'tracking_moved',
      'tracking_stale',
      'tracking_ended',
    ] as const) {
      expect(USER_KINDS).toContain(kind);
      expect(kindLabel(kind)).not.toBe('');
      expect(kindHint(kind)).not.toBe('');
      expect(isAlarm(kind)).toBe(false);
    }
    expect(kindTone('tracking_started')).toBe('error');
    expect(kindTone('tracking_stale')).toBe('paused');
    expect(kindTone('tracking_ended')).toBe('docked');
  });
});

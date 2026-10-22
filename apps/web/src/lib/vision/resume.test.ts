import { describe, expect, it } from 'vitest';

import type { ProposalJob } from './api';
import { jobAreaLabel, resumePlan } from './labels';

const job: ProposalJob = {
  status: 'paused_by_error',
  areas: [
    { id: '11', name: 'Vorderer Garten', status: 'current' },
    { id: '22', name: 'Hinterer Garten', status: 'next' },
  ],
  current_area: 'Vorderer Garten',
  next_area: 'Hinterer Garten',
  progress_percent: 60,
  resumable: true,
  entry_distance_m: 0.8,
  mower_at_entry: true,
};

describe('vision resume texts', () => {
  it('says what approving does: drive first, then the job with its next area', () => {
    expect(resumePlan(1, job, 'de')).toBe(
      'Erst die Fahrschritte unten (Messer aus), dann setzt der Mäher den pausierten Auftrag ' +
        'fort: weiter mit Vorderer Garten, danach Hinterer Garten.',
    );
    expect(resumePlan(1, job, 'en')).toBe(
      'First the drive steps below (blades off), then the mower continues the paused job: ' +
        'on with Vorderer Garten, then Hinterer Garten.',
    );
  });

  it('works without steps, without a next area and without a known job', () => {
    expect(resumePlan(0, { ...job, next_area: null }, 'en')).toBe(
      'Without a maneuver first, the mower continues the paused job: on with Vorderer Garten.',
    );
    expect(resumePlan(0, null, 'de')).toBe(
      'Ohne vorheriges Manöver setzt der Mäher den pausierten Auftrag fort: dort, wo er ' +
        'unterbrochen wurde.',
    );
  });

  it('labels every area status in both languages', () => {
    const statuses = ['done', 'current', 'next', 'planned'] as const;
    expect(statuses.map((s) => jobAreaLabel(s, 'de'))).toEqual([
      'Erledigt',
      'Aktuell',
      'Als Nächstes',
      'Geplant',
    ]);
    expect(statuses.map((s) => jobAreaLabel(s, 'en'))).toEqual([
      'Done',
      'Current',
      'Next',
      'Planned',
    ]);
  });
});

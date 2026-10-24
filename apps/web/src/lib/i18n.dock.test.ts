import { describe, expect, it } from 'vitest';
import { setLocale } from '#lib/paraglide/runtime.js';
import { commandLabel } from '#lib/i18n.js';
import { bulkCommand } from '#lib/fleet/bulk.js';

describe('dock command variants', () => {
  it('labels a dock that ended the job differently', () => {
    setLocale('de', { reload: false });
    expect(commandLabel('return_to_dock', false)).toBe('Zur Station');
    expect(commandLabel('return_to_dock', null)).toBe('Zur Station');
    expect(commandLabel('return_to_dock', true)).toBe('Zur Station (Aufgabe beendet)');
    expect(commandLabel('pause_mow', true)).toBe(commandLabel('pause_mow'));
  });

  it('a bulk dock keeps the jobs', () => {
    expect(bulkCommand('return_to_dock')).toEqual({ kind: 'return_to_dock', end_job: false });
    expect(bulkCommand('pause_mow')).toEqual({ kind: 'pause_mow' });
  });
});

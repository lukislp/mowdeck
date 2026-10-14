import { describe, expect, it } from 'vitest';

import {
  commandClearsPath,
  currentPreview,
  donePercent,
  isJobActivity,
  isRestingActivity,
  loadPathShown,
  previewKey,
  previewOutdated,
  reconcilePath,
  savePathShown,
  shownPath,
  type MowPath,
  type PreviewState,
} from './mow-path';

function path(index: number | null = null, hash = '18446744073709551557'): MowPath {
  return {
    path_hash: hash,
    polylines: [
      [
        [0, 0],
        [4, 0],
      ],
      [
        [4, 0.5],
        [0, 0.5],
      ],
    ],
    kinds: ['edge', 'mow'],
    points_total: 4,
    progress: index === null ? null : { index, direction: 0 },
    area_ids: ['1'],
    planned_at: '2026-10-11T09:00:00Z',
  };
}

function memoryStorage(): Storage {
  const data = new Map<string, string>();
  return {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => void data.set(key, value),
    removeItem: (key) => void data.delete(key),
    clear: () => data.clear(),
    key: () => null,
    get length() {
      return data.size;
    },
  };
}

describe('the path toggle', () => {
  it('is on by default and remembers off', () => {
    const storage = memoryStorage();
    expect(loadPathShown(storage)).toBe(true);
    savePathShown(false, storage);
    expect(loadPathShown(storage)).toBe(false);
    savePathShown(true, storage);
    expect(loadPathShown(storage)).toBe(true);
  });

  it('survives a storage that throws', () => {
    const broken = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
    };
    expect(loadPathShown(broken)).toBe(true);
    expect(() => savePathShown(false, broken)).not.toThrow();
    expect(loadPathShown(null)).toBe(true);
  });
});

describe('isJobActivity', () => {
  it('names the activities of a running job', () => {
    expect(['mowing', 'paused', 'returning'].map(isJobActivity)).toEqual([true, true, true]);
    expect(['docked', 'charging', 'idle', 'error', null].map(isJobActivity)).toEqual([
      false,
      false,
      false,
      false,
      false,
    ]);
  });
});

describe('the preview', () => {
  it('keys on the area order and the pattern, not on key order', () => {
    const a = previewKey(['1', '2'], { name: 'stripes', angle_deg: 30 });
    expect(previewKey(['1', '2'], { angle_deg: 30, name: 'stripes' })).toBe(a);
    expect(previewKey(['2', '1'], { name: 'stripes', angle_deg: 30 })).not.toBe(a);
    expect(previewKey(['1', '2'], { name: 'stripes', angle_deg: 45 })).not.toBe(a);
    expect(previewKey(['1'], null)).toBe('[["1"],null]');
  });

  it('draws only a preview of the current choice and marks older ones outdated', () => {
    const ready: PreviewState = { kind: 'ready', key: 'a', path: path() };
    expect(currentPreview(ready, 'a')).toEqual(path());
    expect(currentPreview(ready, 'b')).toBeNull();
    expect(previewOutdated(ready, 'b')).toBe(true);
    expect(previewOutdated(ready, 'a')).toBe(false);
    expect(previewOutdated({ kind: 'error', key: 'a', reason: 'x' }, 'b')).toBe(true);
    expect(previewOutdated({ kind: 'loading', key: 'a' }, 'b')).toBe(false);
    expect(previewOutdated({ kind: 'idle' }, 'b')).toBe(false);
    expect(currentPreview({ kind: 'idle' }, 'a')).toBeNull();
  });
});

describe('shownPath', () => {
  const live = path(2, 'live');
  const last = path(4, 'last');
  const preview = path(null, 'preview');

  it('prefers the preview, then the running job, then the last job', () => {
    expect(shownPath({ shown: true, preview, live, last })).toEqual({
      path: preview,
      preview: true,
    });
    expect(shownPath({ shown: true, preview: null, live, last })).toEqual({
      path: live,
      preview: false,
    });
    expect(shownPath({ shown: true, preview: null, live: null, last })).toEqual({
      path: last,
      preview: false,
    });
    expect(shownPath({ shown: true, preview: null, live: null, last: null }).path).toBeNull();
  });

  it('shows only an asked for preview while the toggle is off', () => {
    expect(shownPath({ shown: false, preview: null, live, last })).toEqual({
      path: null,
      preview: false,
    });
    expect(shownPath({ shown: false, preview, live, last }).preview).toBe(true);
  });
});

describe('donePercent', () => {
  it('rounds the done share', () => {
    expect(donePercent(path(1))).toBe(25);
    expect(donePercent(path(9))).toBe(100);
    expect(donePercent(path(null))).toBeNull();
    expect(donePercent(null)).toBeNull();
  });
});

describe('last job path rule', () => {
  const job = path(2, 'job');

  it('keeps the path while the job still exists on the mower', () => {
    for (const activity of ['paused', 'returning', 'mowing', 'error', null]) {
      expect(reconcilePath({ live: job, last: job }, null, activity)).toEqual({
        live: null,
        last: job,
      });
    }
  });

  it('clears the path when the gateway has none and the mower is docked or idle', () => {
    for (const activity of ['docked', 'idle', 'charging']) {
      expect(reconcilePath({ live: null, last: job }, null, activity)).toEqual({
        live: null,
        last: null,
      });
    }
  });

  it('takes a polled path as live and last', () => {
    const next = path(3, 'next');
    expect(reconcilePath({ live: null, last: job }, next, 'docked')).toEqual({
      live: next,
      last: next,
    });
  });

  it('does not show the path of a job the owner ended, whatever the mower reports', () => {
    expect(reconcilePath({ live: null, last: null }, job, 'returning', 'job')).toEqual({
      live: null,
      last: null,
    });
    const other = path(0, 'other');
    expect(reconcilePath({ live: null, last: null }, other, 'mowing', 'job').last).toBe(other);
  });

  it('clears on end_job docking and on a start, not on a plain dock', () => {
    expect(commandClearsPath({ kind: 'return_to_dock', end_job: true })).toBe(true);
    expect(commandClearsPath({ kind: 'return_to_dock', end_job: false })).toBe(false);
    expect(commandClearsPath({ kind: 'return_to_dock' })).toBe(false);
    expect(commandClearsPath({ kind: 'start_mow' })).toBe(true);
    expect(commandClearsPath({ kind: 'pause' })).toBe(false);
  });

  it('knows resting activities', () => {
    expect(isRestingActivity('docked')).toBe(true);
    expect(isRestingActivity('returning')).toBe(false);
    expect(isRestingActivity(undefined)).toBe(false);
  });
});

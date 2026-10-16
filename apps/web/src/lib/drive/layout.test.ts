import { describe, expect, it } from 'vitest';

import {
  DRIVE_CAMERA_LAYOUT_KEY,
  THUMB_KEY,
  VIEW_KEY,
  driveView,
  hasCamera,
  loadDriveCameraLayout,
  loadThumb,
  loadView,
  saveDriveCameraLayout,
  saveThumb,
  saveView,
} from './layout';

function memory(): Pick<Storage, 'getItem' | 'setItem'> & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => void data.set(key, value),
  };
}

const broken: Pick<Storage, 'getItem' | 'setItem'> = {
  getItem: () => {
    throw new Error('SecurityError');
  },
  setItem: () => {
    throw new Error('QuotaExceededError');
  },
};

describe('drive view', () => {
  it('knows a camera only from the capability', () => {
    expect(hasCamera({ camera: 'cloud' })).toBe(true);
    expect(hasCamera({ camera: 'local' })).toBe(true);
    expect(hasCamera({ camera: 'none' })).toBe(false);
    expect(hasCamera({ camera: null })).toBe(false);
    expect(hasCamera({})).toBe(false);
    expect(hasCamera(null)).toBe(false);
    expect(hasCamera(undefined)).toBe(false);
  });

  it('shows the camera by default when the mower has one', () => {
    expect(driveView({ camera: 'cloud' }, null, false)).toBe('camera');
  });

  it('keeps the stored choice', () => {
    expect(driveView({ camera: 'cloud' }, 'map', false)).toBe('map');
    expect(driveView({ camera: 'cloud' }, 'camera', false)).toBe('camera');
  });

  it('falls back to the map without a camera or after a camera error', () => {
    expect(driveView({ camera: 'none' }, 'camera', false)).toBe('map');
    expect(driveView({}, null, false)).toBe('map');
    expect(driveView(null, 'camera', false)).toBe('map');
    expect(driveView({ camera: 'cloud' }, 'camera', true)).toBe('map');
    expect(driveView({ camera: 'cloud' }, null, true)).toBe('map');
  });
});

describe('drive view storage', () => {
  it('stores and loads the view', () => {
    const storage = memory();
    expect(loadView(storage)).toBeNull();
    saveView('map', storage);
    expect(storage.data.get(VIEW_KEY)).toBe('map');
    expect(loadView(storage)).toBe('map');
    saveView('camera', storage);
    expect(loadView(storage)).toBe('camera');
    storage.data.set(VIEW_KEY, 'tv');
    expect(loadView(storage)).toBeNull();
  });

  it('shows the map thumbnail unless it was hidden', () => {
    const storage = memory();
    expect(loadThumb(storage)).toBe(true);
    saveThumb(false, storage);
    expect(storage.data.get(THUMB_KEY)).toBe('hidden');
    expect(loadThumb(storage)).toBe(false);
    saveThumb(true, storage);
    expect(loadThumb(storage)).toBe(true);
  });

  it('starts the drive camera on one camera unless both were chosen', () => {
    const storage = memory();
    expect(loadDriveCameraLayout(storage)).toBe('single');
    saveDriveCameraLayout('both', storage);
    expect(storage.data.get(DRIVE_CAMERA_LAYOUT_KEY)).toBe('both');
    expect(loadDriveCameraLayout(storage)).toBe('both');
    saveDriveCameraLayout('single', storage);
    expect(loadDriveCameraLayout(storage)).toBe('single');
  });

  it('survives storage that throws or is missing', () => {
    expect(loadView(broken)).toBeNull();
    expect(loadThumb(broken)).toBe(true);
    expect(loadDriveCameraLayout(broken)).toBe('single');
    expect(() => saveView('map', broken)).not.toThrow();
    expect(() => saveThumb(false, broken)).not.toThrow();
    expect(() => saveDriveCameraLayout('both', broken)).not.toThrow();
    expect(loadView(null)).toBeNull();
    expect(loadThumb(null)).toBe(true);
    expect(() => saveView('map', null)).not.toThrow();
  });

  it('uses localStorage by default', () => {
    localStorage.removeItem(VIEW_KEY);
    saveView('map');
    expect(loadView()).toBe('map');
    localStorage.removeItem(VIEW_KEY);
    saveThumb(false);
    expect(loadThumb()).toBe(false);
    localStorage.removeItem(THUMB_KEY);
    saveDriveCameraLayout('both');
    expect(loadDriveCameraLayout()).toBe('both');
    localStorage.removeItem(DRIVE_CAMERA_LAYOUT_KEY);
  });
});

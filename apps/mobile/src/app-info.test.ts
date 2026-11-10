// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { describe, expect, it } from 'vitest';

import { describeApp, readAppInfo, UNKNOWN_APP } from './app-info';

describe('readAppInfo', () => {
  it('returns the version and build the shell reports', async () => {
    const info = await readAppInfo(() => Promise.resolve({ version: '0.3.12', build: '312' }));
    expect(info).toEqual({ version: '0.3.12', build: '312' });
  });

  it('accepts a numeric build (Android versionCode)', async () => {
    const info = await readAppInfo(() => Promise.resolve({ version: '0.3.12', build: 312 }));
    expect(info.build).toBe('312');
  });

  it('falls back when the plugin is not available (plain browser)', async () => {
    const info = await readAppInfo(() => Promise.reject(new Error('Not implemented on web.')));
    expect(info).toEqual(UNKNOWN_APP);
  });

  it('falls back when the call throws at once', async () => {
    const info = await readAppInfo(() => {
      throw new Error('no plugin');
    });
    expect(info).toEqual(UNKNOWN_APP);
  });

  it('drops values of an odd shape', async () => {
    const info = await readAppInfo(() =>
      Promise.resolve({ version: '<b>1</b>', build: { nested: true } }),
    );
    expect(info).toEqual(UNKNOWN_APP);
  });
});

describe('describeApp', () => {
  it('links the source code of exactly this version', () => {
    expect(describeApp({ version: '0.3.12', build: '312' })).toEqual({
      version: 'v0.3.12',
      build: '312',
      sourceUrl: 'https://github.com/lukislp/mowdeck/tree/v0.3.12',
      sourceLabel: 'github.com/lukislp/mowdeck/tree/v0.3.12',
      sourceIsVersion: true,
    });
  });

  it('shows a neutral fallback and the project without a version', () => {
    expect(describeApp(UNKNOWN_APP)).toEqual({
      version: null,
      build: null,
      sourceUrl: 'https://github.com/lukislp/mowdeck',
      sourceLabel: 'github.com/lukislp/mowdeck',
      sourceIsVersion: false,
    });
  });

  it('keeps a version of another scheme as it is and links the project', () => {
    const app = describeApp({ version: 'dev', build: null });
    expect(app.version).toBe('dev');
    expect(app.sourceIsVersion).toBe(false);
  });
});

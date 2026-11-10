// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { loadNotices, NOTICES_PATH, packageCount, parseNotices } from './notices';

const valid = {
  schema: 1,
  licences: [
    {
      id: 'MIT',
      text: 'Permission is hereby granted',
      packages: [
        { name: 'a', copyright: ['Copyright (c) 2020 A'] },
        { name: 'b', copyright: ['Copyright (c) 2021 B', ''], notice: ' ' },
      ],
    },
    {
      id: 'Apache-2.0',
      text: 'Apache License',
      packages: [{ name: 'c', copyright: ['Copyright 2019 C'], notice: 'C\nby Carol' }],
    },
  ],
};

describe('parseNotices', () => {
  it('reads a complete document', () => {
    const notices = parseNotices(valid)!;
    expect(notices.licences.map((l) => l.id)).toEqual(['MIT', 'Apache-2.0']);
    expect(notices.licences[0]!.packages[1]).toEqual({
      name: 'b',
      copyright: ['Copyright (c) 2021 B'],
      notice: null,
    });
    expect(notices.licences[1]!.packages[0]!.notice).toBe('C\nby Carol');
    expect(packageCount(notices)).toBe(3);
  });

  it.each([
    ['not an object', 'x'],
    ['an array', []],
    ['another schema', { ...valid, schema: 2 }],
    ['no licences', { schema: 1, licences: [] }],
    ['a licence without text', { schema: 1, licences: [{ id: 'MIT', text: ' ', packages: [{}] }] }],
    [
      'a licence without packages',
      { schema: 1, licences: [{ id: 'MIT', text: 't', packages: [] }] },
    ],
    [
      'a package without a copyright line',
      { schema: 1, licences: [{ id: 'MIT', text: 't', packages: [{ name: 'a', copyright: [] }] }] },
    ],
    [
      'a package without a name',
      { schema: 1, licences: [{ id: 'MIT', text: 't', packages: [{ copyright: ['c'] }] }] },
    ],
  ])('refuses %s (a partial list would look complete)', (_label, raw) => {
    expect(parseNotices(raw)).toBeNull();
  });
});

describe('loadNotices', () => {
  it('validates what the fetch returns', async () => {
    expect(await loadNotices(() => Promise.resolve(valid))).not.toBeNull();
    expect(await loadNotices(() => Promise.resolve({ schema: 9 }))).toBeNull();
  });

  it('is null when the file cannot be fetched', async () => {
    expect(await loadNotices(() => Promise.reject(new Error('404')))).toBeNull();
  });
});

describe('the committed notices file', () => {
  const root = resolve(import.meta.dirname, '..');
  const file: unknown = JSON.parse(
    readFileSync(resolve(root, 'public/third-party-notices.json'), 'utf8'),
  );

  it('is a notices file the page can show, with the OFL text of the fonts', () => {
    const notices = parseNotices(file)!;
    expect(notices).not.toBeNull();
    const ofl = notices.licences.find((licence) => licence.id === 'OFL-1.1')!;
    expect(ofl.text).toContain('SIL OPEN FONT LICENSE Version 1.1');
    expect(ofl.packages.map((pkg) => pkg.name)).toContain('@fontsource/ibm-plex-mono');
    expect(packageCount(notices)).toBeGreaterThanOrEqual(10);
  });

  it('is fetched from the path where Vite copies public/', () => {
    expect(NOTICES_PATH).toBe('./third-party-notices.json');
  });
});

describe('the bundled pages', () => {
  const html = (name: string) => readFileSync(resolve(import.meta.dirname, '..', name), 'utf8');

  it('link to the About page from the first-launch screen and the offline page', () => {
    for (const page of ['index.html', 'offline.html']) {
      expect(html(page), page).toMatch(
        /<a id="app-about" href="\.\/about\.html" data-t="aboutApp"/,
      );
    }
  });

  it('build the About page with the elements its script fills', () => {
    const about = html('about.html');
    for (const id of ['version', 'build', 'source', 'source-lead', 'notices', 'back']) {
      expect(about, id).toContain(`id="${id}"`);
    }
    expect(about).toContain('src="./src/about.ts"');
    expect(html('vite.config.ts')).toContain("about: resolve(import.meta.dirname, 'about.html')");
  });
});

import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  buildReleases,
  isUserFacing,
  loadCurated,
  parseCurated,
  parseSubject,
  readReleases,
  subjectText,
} from './whats-new.mjs';

const user = (subject: string) => isUserFacing(parseSubject(subject));

describe('commit subjects', () => {
  it('reads type, scope, breaking mark, text and pull request', () => {
    expect(parseSubject('feat(web)!: add a thing (#12)')).toEqual({
      type: 'feat',
      scope: 'web',
      breaking: true,
      text: 'add a thing',
      pr: 12,
    });
    expect(parseSubject('not conventional')).toBeNull();
  });

  it('keeps feat, fix and perf and drops everything else', () => {
    expect(user('feat(web): a (#1)')).toBe(true);
    expect(user('fix: b (#2)')).toBe(true);
    expect(user('perf(map): c')).toBe(true);
    for (const type of ['ci', 'build', 'chore', 'test', 'docs', 'style', 'refactor']) {
      expect(user(`${type}: x (#3)`)).toBe(false);
    }
  });

  it('drops dependency bumps even when they are typed fix', () => {
    expect(user('fix(deps): bump ruff from 0.17.1 to 0.17.2 (#157)')).toBe(false);
    expect(user('fix(ci): bump docker/login-action from 4.6.0 to 4.7.0 (#160)')).toBe(false);
    expect(user('fix: Bump something from 1 to 2')).toBe(false);
    expect(user('fix(web): bumpy ride')).toBe(true);
  });

  it('capitalises the fallback text', () => {
    expect(subjectText('one section')).toBe('One section');
  });
});

describe('curated entries', () => {
  it('accepts a pull request or a list, with a path link', () => {
    expect(parseCurated({ pr: [1, 2], de: ' a ', en: 'b', href: '/settings#x' })).toEqual({
      prs: [1, 2],
      hide: false,
      de: 'a',
      en: 'b',
      href: '/settings#x',
    });
    expect(parseCurated({ pr: 3, hide: true })).toEqual({ prs: [3], hide: true });
  });

  it('rejects broken entries naming the file', () => {
    expect(() => parseCurated({ de: 'a', en: 'b' }, 'x.json')).toThrow(/x\.json.*pr/);
    expect(() => parseCurated({ pr: 1, de: '', en: 'b' })).toThrow(/"de"/);
    expect(() => parseCurated({ pr: 1, de: 'a' })).toThrow(/"en"/);
    expect(() => parseCurated({ pr: 1, de: 'a', en: 'b', href: 'https://x.org' })).toThrow(/href/);
    expect(() => parseCurated({ pr: 1, de: 'a', en: 'b', href: '//x.org' })).toThrow(/href/);
  });

  it('every curated file of the repository is valid', () => {
    const entries = loadCurated(join(process.cwd(), 'whats-new'));
    expect(entries.length).toBeGreaterThan(0);
  });
});

describe('releases', () => {
  const releases = [
    { version: '0.1.0', date: '2026-01-01', commits: ['feat: first (#1)', 'chore: tidy (#2)'] },
    {
      version: '0.1.1',
      date: '2026-01-02',
      commits: ['fix(deps): bump a from 1 to 2 (#3)', 'ci: x (#4)'],
    },
    {
      version: '0.2.0',
      date: '2026-01-03',
      commits: ['feat(web): second thing (#5)', 'fix: third (#6)'],
    },
  ];
  const curated = [parseCurated({ pr: 5, de: 'Zweites', en: 'Second', href: '/mowers' })];

  it('aggregates newest first and skips releases without user-facing changes', () => {
    const out = buildReleases(releases, curated);
    expect(out.map((release) => release.version)).toEqual(['0.2.0', '0.1.0']);
  });

  it('uses the curated de/en text and falls back to the English subject', () => {
    const [latest, first] = buildReleases(releases, curated);
    expect(latest?.entries).toEqual([
      { id: 'pr-5', kind: 'feature', en: 'Second', de: 'Zweites', href: '/mowers' },
      { id: 'pr-6', kind: 'fix', en: 'Third' },
    ]);
    expect(first?.entries[0]).toEqual({ id: 'pr-1', kind: 'feature', en: 'First' });
  });

  it('hides a commit marked hide and shows a multi-PR entry once, in the newest release', () => {
    const out = buildReleases(releases, [
      parseCurated({ pr: 6, hide: true }),
      parseCurated({ pr: [1, 5], de: 'Beides', en: 'Both' }),
    ]);
    expect(out).toHaveLength(1);
    expect(out[0]?.entries.map((entry) => entry.id)).toEqual(['pr-1']);
    expect(out[0]?.version).toBe('0.2.0');
  });

  it('keeps only the newest releases', () => {
    expect(buildReleases(releases, [], 1)).toHaveLength(1);
  });
});

describe('git history', () => {
  it('reads tag ranges and adds an untagged version', () => {
    const dir = mkdtempSync(join(tmpdir(), 'whats-new-'));
    try {
      const git = (...args: string[]) =>
        execFileSync(
          'git',
          [
            '-c',
            'user.email=t@example.org',
            '-c',
            'user.name=t',
            '-c',
            'commit.gpgsign=false',
            '-c',
            'tag.gpgsign=false',
            ...args,
          ],
          { cwd: dir, encoding: 'utf8' },
        );
      git('init', '-q');
      for (const [file, subject, tag] of [
        ['a', 'feat: one (#1)', 'v0.1.0'],
        ['b', 'fix: two (#2)', 'v0.1.1'],
        ['c', 'feat: three (#3)', ''],
      ] as const) {
        writeFileSync(join(dir, file), subject);
        git('add', '.');
        git('commit', '-q', '-m', subject);
        if (tag) git('tag', tag);
      }
      const read = readReleases('0.2.0', dir);
      expect(read.map((release) => [release.version, release.commits])).toEqual([
        ['0.1.0', ['feat: one (#1)']],
        ['0.1.1', ['fix: two (#2)']],
        ['0.2.0', ['feat: three (#3)']],
      ]);
      expect(readReleases('0.1.1', dir)).toHaveLength(2);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }, 60_000);
});

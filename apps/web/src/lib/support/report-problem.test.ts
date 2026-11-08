// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { DEFAULT_SOURCE, OFFICIAL_REPOSITORY, type SourceInfo } from '#lib/legal/source.js';

import { gatherEnvironment } from './environment';
import {
  FORM_FIELDS,
  MAX_URL_LENGTH,
  clip,
  codespacesSteps,
  environmentText,
  mowerText,
  parseUserAgent,
  reportProblemUrl,
  versionText,
  type ReportProblemInput,
} from './report-problem';

const RELEASE: SourceInfo = { repository: OFFICIAL_REPOSITORY, version: '0.60.3', ref: null };

function input(over: Partial<ReportProblemInput> = {}): ReportProblemInput {
  return {
    source: RELEASE,
    platform: 'web',
    browser: 'Chrome 142',
    os: 'Windows',
    locale: 'de',
    demo: false,
    mower: { model: 'LUBA 2 AWD X', link: 'mammotion' },
    ...over,
  };
}

function params(url: string): URLSearchParams {
  return new URL(url).searchParams;
}

describe('environmentText', () => {
  it('lists app, platform, locale, demo and the Codespaces link of a release', () => {
    expect(environmentText(input())).toBe(
      [
        'App: 0.60.3 (source github.com/lukislp/mowdeck/tree/v0.60.3)',
        'Platform: web, Chrome 142 on Windows',
        'Locale: de',
        'Demo: no',
        'Reproduce in Codespaces: https://codespaces.new/lukislp/mowdeck, then run: git checkout v0.60.3 && .devcontainer/post-start.sh',
      ].join('\n'),
    );
  });

  it('leaves out the Codespaces line for a development build', () => {
    const text = environmentText(input({ source: DEFAULT_SOURCE }));
    expect(text).toContain('App: dev build (source github.com/lukislp/mowdeck)');
    expect(text).not.toContain('Codespaces');
  });

  it('names the commit of a development build', () => {
    const source = { ...DEFAULT_SOURCE, ref: 'abc1234' };
    expect(versionText(source)).toBe('dev build (abc1234)');
    expect(codespacesSteps(source)).toBeNull();
  });

  it('names the app shells and the demo', () => {
    expect(environmentText(input({ platform: 'ios', demo: true }))).toContain(
      'Platform: iOS app shell, Chrome 142 on Windows',
    );
    expect(environmentText(input({ platform: 'android', demo: true }))).toContain('Demo: yes');
  });

  it('copes with an unknown browser and OS', () => {
    const text = environmentText(input({ browser: null, os: null, locale: '' }));
    expect(text).toContain('Platform: web\n');
    expect(text).toContain('Locale: unknown');
  });

  it('offers no Codespace for a fork or a non-semver version', () => {
    expect(codespacesSteps({ ...RELEASE, repository: 'https://example.org/fork' })).toBeNull();
    expect(codespacesSteps({ ...RELEASE, version: 'nightly' })).toBeNull();
  });

  it('opens a plain Codespace and checks out the release afterwards', () => {
    // codespaces.new ignores a ref query parameter and a Codespace always starts from a branch.
    const steps = codespacesSteps({ ...RELEASE, version: 'v1.2.3' });
    expect(steps?.url).toBe('https://codespaces.new/lukislp/mowdeck');
    expect(steps?.url).not.toContain('?');
    expect(steps?.commands).toBe('git checkout v1.2.3 && .devcontainer/post-start.sh');
  });

  it('keeps line breaks in values from adding lines', () => {
    const text = environmentText(input({ locale: 'de\nDemo: yes' }));
    expect(text.split('\n')).toHaveLength(5);
  });
});

describe('mowerText', () => {
  it('joins model and link', () => {
    expect(mowerText({ model: 'LUBA 2 AWD X', link: 'mammotion' })).toBe(
      'LUBA 2 AWD X, link: mammotion',
    );
  });

  it('handles partial and missing data', () => {
    expect(mowerText(null)).toBe('');
    expect(mowerText({ model: null, link: null })).toBe('');
    expect(mowerText({ model: 'X', link: null })).toBe('X');
    expect(mowerText({ model: null, link: 'simulator' })).toBe('unknown model, link: simulator');
  });
});

describe('reportProblemUrl', () => {
  it('opens the bug report form of the official repository', () => {
    const url = reportProblemUrl(input());
    expect(url.startsWith('https://github.com/lukislp/mowdeck/issues/new?')).toBe(true);
    const query = params(url);
    expect(query.get('template')).toBe('bug_report.yml');
    expect(query.get('version')).toBe('0.60.3');
    expect(query.get('mower')).toBe('LUBA 2 AWD X, link: mammotion');
    expect(query.get('environment')).toBe(environmentText(input()));
  });

  it('percent-encodes values, spaces as %20 and no plus signs', () => {
    const url = reportProblemUrl(input({ mower: { model: 'A & B+C', link: 'x' } }));
    expect(url).toContain('mower=A%20%26%20B%2BC%2C%20link%3A%20x');
    expect(url).not.toContain('+');
    expect(params(url).get('mower')).toBe('A & B+C, link: x');
    expect(url).not.toContain('\n');
  });

  it('omits mower when none is known and stays usable for a development build', () => {
    const url = reportProblemUrl(input({ mower: null, source: DEFAULT_SOURCE }));
    const query = params(url);
    expect(query.has('mower')).toBe(false);
    expect(query.get('version')).toBe('dev build');
    expect(query.get('environment')).not.toContain('Codespaces');
  });

  it('truncates long values and keeps the address short', () => {
    const long = 'ä'.repeat(5000);
    const url = reportProblemUrl(
      input({ mower: { model: long, link: long }, browser: long, os: long, locale: long }),
    );
    expect(url.length).toBeLessThanOrEqual(MAX_URL_LENGTH);
    expect(params(url).get('mower')?.length).toBeLessThanOrEqual(200);
    expect(params(url).get('mower')?.endsWith('…')).toBe(true);
  });

  it('shortens the environment, not the other fields, to fit', () => {
    const url = reportProblemUrl(input({ browser: '😀'.repeat(900), os: null }));
    expect(url.length).toBeLessThanOrEqual(MAX_URL_LENGTH);
    expect(params(url).get('version')).toBe('0.60.3');
    expect(() => decodeURIComponent(url)).not.toThrow();
  });

  it('contains nothing but environment data', () => {
    const url = decodeURIComponent(reportProblemUrl(input()));
    for (const secret of ['token', 'serial', 'password', 'latitude', 'mower_id']) {
      expect(url.toLowerCase()).not.toContain(secret);
    }
  });
});

describe('clip', () => {
  it('cuts with an ellipsis and never inside a surrogate pair', () => {
    expect(clip('  short ', 10)).toBe('short');
    expect(clip('abcdef', 4)).toBe('abc…');
    expect(clip('ab😀😀', 4)).toBe('ab…');
  });
});

describe('parseUserAgent', () => {
  it.each([
    [
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
      'Chrome 142',
      'Windows',
    ],
    [
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0',
      'Edge 142',
      'Windows',
    ],
    [
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15',
      'Safari 18',
      'macOS',
    ],
    [
      'Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0',
      'Firefox 133',
      'Linux',
    ],
    [
      'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1',
      'Safari 18',
      'iOS',
    ],
    [
      'Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Mobile Safari/537.36',
      'Chrome 142',
      'Android',
    ],
    [
      'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148',
      'WebKit',
      'iOS',
    ],
    ['curl/8.0', null, null],
    ['', null, null],
  ])('%s', (ua, browser, os) => {
    expect(parseUserAgent(ua)).toEqual({ browser, os });
  });
});

describe('gatherEnvironment', () => {
  const base = {
    source: RELEASE,
    userAgent: 'Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0',
    shellPlatform: null,
    locale: 'en',
    demo: true,
  };

  it('builds the input from the injected values', () => {
    expect(
      gatherEnvironment({ ...base, mower: { model: 'M', connection_kind: 'simulator' } }),
    ).toEqual({
      source: RELEASE,
      platform: 'web',
      browser: 'Firefox 133',
      os: 'Linux',
      locale: 'en',
      demo: true,
      mower: { model: 'M', link: 'simulator' },
    });
  });

  it('uses the shell platform and no mower by default', () => {
    const env = gatherEnvironment({ ...base, shellPlatform: 'ios' });
    expect(env.platform).toBe('ios');
    expect(env.mower).toBeNull();
  });
});

describe('the bug report form', () => {
  // vitest runs in apps/web
  const yaml = readFileSync(
    resolve(process.cwd(), '../../.github/ISSUE_TEMPLATE/bug_report.yml'),
    'utf8',
  );
  const ids = [...yaml.matchAll(/^\s+id:\s*(\S+)\s*$/gm)].map((match) => match[1]);

  it('has every field the address fills, and the ids old links use', () => {
    for (const id of Object.values(FORM_FIELDS)) expect(ids).toContain(id);
    for (const id of ['what', 'version', 'mower', 'logs']) expect(ids).toContain(id);
  });

  it('describes the environment field', () => {
    expect(yaml).toContain('label: Environment');
    expect(yaml).toContain("Filled in by the app's Report a problem button; edit if needed.");
  });
});

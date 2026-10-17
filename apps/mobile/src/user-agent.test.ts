/**
 * The iOS shell's user agent (`ios/App/App/MainViewController.swift`, `ShellUserAgent`): the Agora
 * Web SDK of the live camera classifies the browser with its bundled ua-parser-js. A plain
 * WKWebView user agent parses as an unknown "WebKit" browser, which gets a capability profile of
 * its own; with Safari's tokens the shell parses as Mobile Safari, like Safari on the same phone.
 * The template is read from the Swift source, so a change there is checked here.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import UAParser from 'ua-parser-js';
import { describe, expect, it } from 'vitest';

const swift = readFileSync(
  resolve(import.meta.dirname, '../ios/App/App/MainViewController.swift'),
  'utf8',
);

/** The `Version/... Safari/604.1` template of `ShellUserAgent.applicationName`. */
function template(): string {
  const match = /let safari = "(Version\/\\\(safariVersion\(systemVersion\)\)[^"]*)"/.exec(swift);
  if (!match?.[1]) throw new Error('ShellUserAgent template not found in MainViewController.swift');
  return match[1];
}

/** What WKWebView reports: its fixed prefix plus the application name. */
function userAgent(ios: string, applicationName: string): string {
  const os = ios.replace(/\./g, '_');
  return (
    `Mozilla/5.0 (iPhone; CPU iPhone OS ${os} like Mac OS X) AppleWebKit/605.1.15 ` +
    `(KHTML, like Gecko) ${applicationName}`
  );
}

function shellUserAgent(ios: string): string {
  const [major = '18', minor = '0'] = ios.split('.');
  const name = template().replace('\\(safariVersion(systemVersion))', `${major}.${minor}`);
  return userAgent(ios, `${name} MowDeckApp`);
}

describe('iOS shell user agent', () => {
  it('a plain WKWebView user agent is no known browser (the bug)', () => {
    const result = new UAParser(userAgent('18.6', 'Mobile/15E148 MowDeckApp')).getResult();
    expect(result.browser.name).toBe('WebKit');
  });

  it.each(['18.6', '18.6.2', '17.5.1', '26.0'])(
    'parses as Mobile Safari on iOS %s and keeps the MowDeckApp token',
    (ios) => {
      const ua = shellUserAgent(ios);
      const result = new UAParser(ua).getResult();
      expect(result.browser.name).toBe('Mobile Safari');
      expect(result.browser.major).toBe(ios.split('.')[0]);
      expect(result.os.name).toBe('iOS');
      expect(ua.endsWith(' MowDeckApp')).toBe(true);
    },
  );

  it('matches the user agent of Safari itself apart from the app token', () => {
    const safari =
      'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 ' +
      '(KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1';
    expect(shellUserAgent('18.6')).toBe(`${safari} MowDeckApp`);
  });
});

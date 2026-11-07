// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * "Report a problem": the address of the repository's bug report form with the environment
 * fields filled in from the running app. GitHub fills an issue form from query parameters named
 * after the field ids of `.github/ISSUE_TEMPLATE/bug_report.yml` (`version`, `mower`,
 * `environment`); a test reads the YAML so the form and this code cannot drift apart.
 *
 * Privacy: only environment data goes into the address (app version, platform, browser, OS,
 * locale, demo or not, and the model and link kind of one mower). Never a token, an account or
 * user name, a serial number, a position, a mower id or a log line. The person reading the form
 * can see and edit everything before anything is sent.
 */
import {
  OFFICIAL_REPOSITORY,
  displayUrl,
  releaseRef,
  sourceCodeUrl,
  type SourceInfo,
} from '#lib/legal/source.js';

export const NEW_ISSUE_URL = `${OFFICIAL_REPOSITORY}/issues/new`;
export const BUG_TEMPLATE = 'bug_report.yml';
/** The field ids of the bug report form that this feature fills. */
export const FORM_FIELDS = {
  version: 'version',
  mower: 'mower',
  environment: 'environment',
} as const;
/** Upper bound of the whole address; browsers and GitHub cope with far more, proxies may not. */
export const MAX_URL_LENGTH = 6000;

const MAX_VERSION = 100;
const MAX_MOWER = 200;
const MAX_ENVIRONMENT = 1500;

export type ClientPlatform = 'web' | 'ios' | 'android';

export interface ReportProblemInput {
  source: SourceInfo;
  platform: ClientPlatform;
  /** Browser name and major version (`Chrome 142`), null when unknown. */
  browser: string | null;
  /** Operating system (`Windows`), null when unknown. */
  os: string | null;
  locale: string;
  demo: boolean;
  /** Model and link kind of the first mower the app knows; null when there is none. */
  mower: { model: string | null; link: string | null } | null;
}

/** Cut `value` to `max` characters with an ellipsis. */
export function clip(value: string, max: number): string {
  const text = value.trim();
  if (text.length <= max) return text;
  // Do not cut a surrogate pair in half: it would encode to an invalid sequence.
  const end = /[\uD800-\uDBFF]/.test(text.charAt(max - 2)) ? max - 2 : max - 1;
  return `${text.slice(0, end)}…`;
}

/** One line without line breaks or control characters, so a value cannot add fields. */
function oneLine(value: string): string {
  // eslint-disable-next-line no-control-regex -- stripping control characters is the point
  return value.replace(/[\u0000-\u001f\u007f]+/g, ' ').trim();
}

/** The value of the form's "Version" field: `0.60.3`, or `dev build (<ref>)`. */
export function versionText(source: SourceInfo): string {
  if (source.version) return source.version.replace(/^v/, '');
  return source.ref ? `dev build (${source.ref})` : 'dev build';
}

/** The Codespaces link of the exact release; null without a release version. */
export function codespacesUrl(source: SourceInfo): string | null {
  const ref = releaseRef(source.version);
  // Only for the official repository: a fork's tags are not ours to open in a Codespace.
  if (!ref || source.repository !== OFFICIAL_REPOSITORY) return null;
  const path = OFFICIAL_REPOSITORY.replace('https://github.com/', '');
  return `https://codespaces.new/${path}?ref=${encodeURIComponent(ref)}&quickstart=1`;
}

const PLATFORM_NAME: Record<ClientPlatform, string> = {
  web: 'web',
  ios: 'iOS app shell',
  android: 'Android app shell',
};

/** The multi-line text of the form's "Environment" field. */
export function environmentText(input: ReportProblemInput): string {
  const { source } = input;
  const client = [input.browser, input.os && `on ${input.os}`].filter(Boolean).join(' ');
  const lines = [
    `App: ${versionText(source)} (source ${displayUrl(sourceCodeUrl(source))})`,
    `Platform: ${PLATFORM_NAME[input.platform]}${client ? `, ${client}` : ''}`,
    `Locale: ${oneLine(input.locale) || 'unknown'}`,
    `Demo: ${input.demo ? 'yes' : 'no'}`,
  ];
  const codespaces = codespacesUrl(source);
  if (codespaces) lines.push(`Reproduce in Codespaces: ${codespaces}`);
  return lines.map(oneLine).join('\n');
}

/** The text of the form's "Mower and link" field, empty when no mower is known. */
export function mowerText(mower: ReportProblemInput['mower']): string {
  if (!mower) return '';
  const model = oneLine(mower.model ?? '');
  const link = oneLine(mower.link ?? '');
  if (!model && !link) return '';
  return link ? `${model || 'unknown model'}, link: ${link}` : model;
}

/** The address of the bug report form, filled in. Never longer than `MAX_URL_LENGTH`. */
export function reportProblemUrl(input: ReportProblemInput): string {
  const version = clip(oneLine(versionText(input.source)), MAX_VERSION);
  const mower = clip(mowerText(input.mower), MAX_MOWER);
  let environment = clip(environmentText(input), MAX_ENVIRONMENT);
  const build = (): string => {
    const query = new URLSearchParams({ template: BUG_TEMPLATE });
    query.set(FORM_FIELDS.version, version);
    if (mower) query.set(FORM_FIELDS.mower, mower);
    if (environment) query.set(FORM_FIELDS.environment, environment);
    // URLSearchParams writes a space as "+": use %20 so no field shows a stray plus sign.
    return `${NEW_ISSUE_URL}?${query.toString().replaceAll('+', '%20')}`;
  };
  let url = build();
  // Multi-byte text encodes up to twelve times longer: shorten the environment until it fits.
  while (url.length > MAX_URL_LENGTH && environment.length > 0) {
    environment = clip(environment, Math.floor(environment.length / 2));
    if (environment.length <= 1) environment = '';
    url = build();
  }
  return url;
}

/** Browser name and OS from a user agent string; the parts are null when not recognised. */
export function parseUserAgent(ua: string): { browser: string | null; os: string | null } {
  const major = (pattern: RegExp): string | null => pattern.exec(ua)?.[1] ?? null;
  let browser: string | null = null;
  const edge = major(/\bEdgA?\/(\d+)/);
  const opera = major(/\bOPR\/(\d+)/);
  const firefox = major(/\b(?:Firefox|FxiOS)\/(\d+)/);
  const chrome = major(/\b(?:Chrome|CriOS)\/(\d+)/);
  const safari = major(/\bVersion\/(\d+)[\d.]*.*Safari\//);
  if (edge) browser = `Edge ${edge}`;
  else if (opera) browser = `Opera ${opera}`;
  else if (firefox) browser = `Firefox ${firefox}`;
  else if (chrome) browser = `Chrome ${chrome}`;
  else if (safari) browser = `Safari ${safari}`;
  else if (/AppleWebKit\//.test(ua)) browser = 'WebKit';

  let os: string | null = null;
  if (/\bAndroid\b/.test(ua)) os = 'Android';
  else if (/\b(iPhone|iPad|iPod)\b/.test(ua)) os = 'iOS';
  else if (/\bWindows\b/.test(ua)) os = 'Windows';
  else if (/\bCrOS\b/.test(ua)) os = 'ChromeOS';
  else if (/\bMac OS X\b|\bMacintosh\b/.test(ua)) os = 'macOS';
  else if (/\bLinux\b|\bX11\b/.test(ua)) os = 'Linux';
  return { browser, os };
}

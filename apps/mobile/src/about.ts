// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * The About page (about.html), reachable from the first-launch screen and the offline page: the
 * app's version, licence, source code and the third-party notices. Wiring only; the logic is in
 * `app-info.ts`, `source-link.ts` and `notices.ts`.
 */
import '@mowdeck/ui/styles/fonts.css';
import '@mowdeck/ui/styles/tokens.css';
import '@mowdeck/ui/styles/base.css';
import './pages.css';

import { App } from '@capacitor/app';

import { describeApp, readAppInfo, UNKNOWN_APP, type AppInfo } from './app-info';
import { startLanguage, type TextKey } from './i18n';
import { languageStorage } from './language-storage';
import { loadNotices, NOTICES_PATH, type Notices } from './notices';
import { applyPrivacyLink } from './project-links';

type Texts = Record<TextKey, string>;

const version = document.querySelector<HTMLElement>('#version')!;
const build = document.querySelector<HTMLElement>('#build')!;
const sourceLead = document.querySelector<HTMLElement>('#source-lead')!;
const source = document.querySelector<HTMLAnchorElement>('#source')!;
const container = document.querySelector<HTMLElement>('#notices')!;
const privacyPolicy = document.querySelector<HTMLAnchorElement>('#privacy-policy');

let info: AppInfo = UNKNOWN_APP;
let notices: Notices | null | undefined;

function renderApp(t: Texts): void {
  const app = describeApp(info);
  version.textContent = app.version ?? t.aboutVersionUnknown;
  build.textContent = app.build ?? t.aboutVersionUnknown;
  sourceLead.textContent = app.sourceIsVersion ? t.aboutSourceVersion : t.aboutSourceProject;
  source.href = app.sourceUrl;
  source.textContent = app.sourceLabel;
}

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  text?: string,
  className?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function renderNotices(t: Texts): void {
  container.replaceChildren();
  if (notices === undefined) {
    container.append(element('p', t.aboutNoticesLoading, 'hint'));
    return;
  }
  if (notices === null) {
    container.append(element('p', t.aboutNoticesError, 'message'));
    return;
  }
  for (const licence of notices.licences) {
    const group = element('section', undefined, 'licence');
    group.append(element('h3', licence.id, 'licence-id'));
    const list = element('ul', undefined, 'packages');
    for (const pkg of licence.packages) {
      const item = element('li');
      item.append(element('span', pkg.name, 'package'));
      for (const line of pkg.copyright) item.append(element('span', line, 'copyright'));
      if (pkg.notice) item.append(element('pre', pkg.notice, 'notice-text'));
      list.append(item);
    }
    const text = element('details');
    text.append(element('summary', t.aboutLicenceText), element('pre', licence.text));
    group.append(list, text);
    container.append(group);
  }
}

const page = startLanguage(document, languageStorage(), navigator.language, (t, language) => {
  applyPrivacyLink(privacyPolicy, language);
  renderApp(t);
  renderNotices(t);
});

document.querySelector('#back')!.addEventListener('click', () => {
  if (history.length > 1) history.back();
  else location.assign('./index.html');
});

void readAppInfo(() => App.getInfo()).then((result) => {
  info = result;
  renderApp(page.texts());
});

void loadNotices(async () => {
  const response = await fetch(NOTICES_PATH);
  if (!response.ok) throw new Error(`notices: HTTP ${response.status}`);
  return response.json() as Promise<unknown>;
}).then((result) => {
  notices = result;
  renderNotices(page.texts());
});

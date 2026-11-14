// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/** The first-launch screen (index.html): asks for the server address and hands it to the shell. */
import '@mowdeck/ui/styles/fonts.css';
import '@mowdeck/ui/styles/tokens.css';
import '@mowdeck/ui/styles/base.css';
import './pages.css';

import { CapacitorHttp } from '@capacitor/core';

import { checkServer, type ConnectResult } from './connect';
import { DEMO_SERVER_URL } from './demo-server';
import { fill, startLanguage } from './i18n';
import { languageStorage } from './language-storage';
import { applyPrivacyLink } from './project-links';
import { MowDeckShell } from './shell';

const privacyLink = document.querySelector<HTMLAnchorElement>('#privacy');
const page = startLanguage(document, languageStorage(), navigator.language, (_, language) => {
  applyPrivacyLink(privacyLink, language);
});

const form = document.querySelector<HTMLFormElement>('#connect')!;
const input = document.querySelector<HTMLInputElement>('#server')!;
const submit = document.querySelector<HTMLButtonElement>('#submit')!;
const message = document.querySelector<HTMLParagraphElement>('#message')!;
const anyway = document.querySelector<HTMLButtonElement>('#anyway')!;
const demo = document.querySelector<HTMLButtonElement>('#demo')!;
const confirmation = document.querySelector<HTMLDivElement>('#confirm')!;
const confirmTitle = document.querySelector<HTMLParagraphElement>('#confirm-title')!;
const confirmBody = document.querySelector<HTMLParagraphElement>('#confirm-body')!;
const confirmOk = document.querySelector<HTMLButtonElement>('#confirm-ok')!;
const confirmCancel = document.querySelector<HTMLButtonElement>('#confirm-cancel')!;

/** The unreachable address "Use anyway" would offer, and the address a confirmation would open. */
let unreachable: string | null = null;
let pending: string | null = null;

function show(text: string, tone: 'error' | 'warning' | 'none'): void {
  message.textContent = text;
  message.dataset.tone = tone;
  message.hidden = tone === 'none';
  input.setAttribute('aria-invalid', String(tone === 'error'));
}

async function use(origin: string): Promise<void> {
  submit.disabled = true;
  await MowDeckShell.setServerUrl({ url: origin });
}

function hideConfirm(): void {
  confirmation.hidden = true;
  pending = null;
}

/** Ask the user to confirm an address; only a confirmed one is ever handed to the shell. */
function askToConfirm(origin: string, title: string, body: string, action: string): void {
  pending = origin;
  confirmTitle.textContent = title;
  confirmBody.textContent = body;
  confirmBody.hidden = body === '';
  confirmOk.textContent = action;
  confirmation.hidden = false;
  confirmOk.focus();
}

/** Render a verdict (the rule itself is `decideServer`); this only maps it to the page. */
function render(result: ConnectResult): void {
  anyway.hidden = true;
  unreachable = null;
  hideConfirm();
  const t = page.texts();
  switch (result.kind) {
    case 'invalid':
      show(t[result.problem], 'error');
      break;
    case 'not-mowdeck':
      show(t.notMowDeck, 'error');
      break;
    case 'unreachable':
      unreachable = result.origin;
      anyway.hidden = false;
      show(t.unreachable + (result.insecure ? ` ${t.insecure}` : ''), 'error');
      break;
    case 'redirected':
      askToConfirm(
        result.origin,
        fill(t.redirected, { host: result.host }),
        '',
        fill(t.redirectedOpen, { host: result.host }),
      );
      break;
    case 'connect':
      if (result.insecure) show(t.insecure, 'warning');
      void use(result.origin);
      break;
  }
}

/** The one connect flow: "Connect" hands it the typed address, "Try the demo" the demo's. */
function connect(address: string): void {
  submit.disabled = true;
  demo.disabled = true;
  submit.textContent = page.texts().checking;
  show('', 'none');
  anyway.hidden = true;
  hideConfirm();
  void checkServer(address, async (url) => {
    const response = await CapacitorHttp.get({ url, connectTimeout: 8000, readTimeout: 8000 });
    const headers = response.headers as Record<string, string> | undefined;
    return {
      status: response.status,
      data: response.data as unknown,
      url: response.url,
      location: headers?.location ?? headers?.Location,
    };
  })
    .then(render)
    .finally(() => {
      submit.disabled = false;
      demo.disabled = false;
      submit.textContent = page.texts().connect;
    });
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  connect(input.value);
});

demo.addEventListener('click', () => {
  input.value = DEMO_SERVER_URL;
  connect(DEMO_SERVER_URL);
});

anyway.addEventListener('click', () => {
  if (!unreachable) return;
  const t = page.texts();
  const host = new URL(unreachable).host;
  askToConfirm(
    unreachable,
    fill(t.anywayTitle, { host }),
    fill(t.anywayBody, { host }),
    fill(t.anywayOpen, { host }),
  );
});

confirmOk.addEventListener('click', () => {
  if (pending) void use(pending);
});

confirmCancel.addEventListener('click', () => {
  hideConfirm();
  (anyway.hidden ? input : anyway).focus();
});

void MowDeckShell.getServerUrl()
  .then(({ url }) => {
    if (url) input.value = url;
  })
  .catch(() => undefined);
input.focus();

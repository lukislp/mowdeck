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
import { startLanguage } from './i18n';
import { languageStorage } from './language-storage';
import { MowDeckShell } from './shell';

const page = startLanguage(document, languageStorage(), navigator.language);

const form = document.querySelector<HTMLFormElement>('#connect')!;
const input = document.querySelector<HTMLInputElement>('#server')!;
const submit = document.querySelector<HTMLButtonElement>('#submit')!;
const message = document.querySelector<HTMLParagraphElement>('#message')!;
const anyway = document.querySelector<HTMLButtonElement>('#anyway')!;
const demo = document.querySelector<HTMLButtonElement>('#demo')!;

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

function render(result: ConnectResult): void {
  anyway.hidden = true;
  pending = null;
  switch (result.kind) {
    case 'invalid':
      show(page.texts()[result.problem], 'error');
      break;
    case 'unreachable':
      pending = result.origin;
      anyway.hidden = false;
      show(
        page.texts().unreachable + (result.insecure ? ` ${page.texts().insecure}` : ''),
        'error',
      );
      break;
    case 'ok':
      if (result.insecure) show(page.texts().insecure, 'warning');
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
  void checkServer(address, async (url) => {
    const response = await CapacitorHttp.get({ url, connectTimeout: 8000, readTimeout: 8000 });
    return { status: response.status, data: response.data as unknown };
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
  if (pending) void use(pending);
});

void MowDeckShell.getServerUrl()
  .then(({ url }) => {
    if (url) input.value = url;
  })
  .catch(() => undefined);
input.focus();

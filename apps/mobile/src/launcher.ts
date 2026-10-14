/** The first-launch screen (index.html): asks for the server address and hands it to the shell. */
import '@mowdeck/ui/styles/fonts.css';
import '@mowdeck/ui/styles/tokens.css';
import '@mowdeck/ui/styles/base.css';
import './pages.css';

import { CapacitorHttp } from '@capacitor/core';

import { checkServer, type ConnectResult } from './connect';
import { applyTexts, texts } from './i18n';
import { MowDeckShell } from './shell';

const language = navigator.language ?? 'en';
const t = texts(language);
applyTexts(document, t, language);

const form = document.querySelector<HTMLFormElement>('#connect')!;
const input = document.querySelector<HTMLInputElement>('#server')!;
const submit = document.querySelector<HTMLButtonElement>('#submit')!;
const message = document.querySelector<HTMLParagraphElement>('#message')!;
const anyway = document.querySelector<HTMLButtonElement>('#anyway')!;

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
      show(t[result.problem], 'error');
      break;
    case 'unreachable':
      pending = result.origin;
      anyway.hidden = false;
      show(t.unreachable + (result.insecure ? ` ${t.insecure}` : ''), 'error');
      break;
    case 'ok':
      if (result.insecure) show(t.insecure, 'warning');
      void use(result.origin);
      break;
  }
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  submit.disabled = true;
  submit.textContent = t.checking;
  show('', 'none');
  void checkServer(input.value, async (url) => {
    const response = await CapacitorHttp.get({ url, connectTimeout: 8000, readTimeout: 8000 });
    return { status: response.status, data: response.data as unknown };
  })
    .then(render)
    .finally(() => {
      submit.disabled = false;
      submit.textContent = t.connect;
    });
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

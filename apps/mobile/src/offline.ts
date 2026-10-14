/** The page the shell shows when the stored server cannot be reached (`server.errorPath`). */
import '@mowdeck/ui/styles/fonts.css';
import '@mowdeck/ui/styles/tokens.css';
import '@mowdeck/ui/styles/base.css';
import './pages.css';

import { applyTexts, texts } from './i18n';
import { MowDeckShell } from './shell';

const language = navigator.language ?? 'en';
applyTexts(document, texts(language), language);

const server = document.querySelector<HTMLElement>('#server')!;
void MowDeckShell.getServerUrl()
  .then(({ url }) => {
    server.textContent = url ?? '';
  })
  .catch(() => undefined);

document.querySelector('#retry')!.addEventListener('click', () => void MowDeckShell.reload());
document
  .querySelector('#change')!
  .addEventListener('click', () => void MowDeckShell.resetServerUrl());

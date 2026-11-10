// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/** The page the shell shows when the stored server cannot be reached (`server.errorPath`). */
import '@mowdeck/ui/styles/fonts.css';
import '@mowdeck/ui/styles/tokens.css';
import '@mowdeck/ui/styles/base.css';
import './pages.css';

import { startLanguage } from './i18n';
import { languageStorage } from './language-storage';
import { MowDeckShell } from './shell';

startLanguage(document, languageStorage(), navigator.language);

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

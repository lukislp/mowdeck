// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * Texts of the bundled pages (German or English). The language is the stored choice of the
 * toggle on the page, else the system language. The bundled origin's localStorage is not shared
 * with the server's web app, which has its own switch (also on its sign-in page).
 */

const de = {
  title: 'MowDeck verbinden',
  lead: 'Gib die Adresse deines MowDeck-Servers ein. Die App lädt die Oberfläche direkt von dort.',
  explain: 'MowDeck ist selbst gehostet: Die App braucht deinen eigenen MowDeck-Server.',
  label: 'Server-Adresse',
  hint: 'zum Beispiel mowdeck.home.lan',
  connect: 'Verbinden',
  checking: 'Prüfe …',
  empty: 'Bitte eine Adresse eingeben.',
  invalid: 'Das ist keine gültige Adresse.',
  scheme: 'Nur https:// oder http:// ist möglich.',
  path: 'Nur die Adresse ohne Pfad, zum Beispiel https://mowdeck.home.lan',
  credentials: 'Die Adresse darf keinen Benutzernamen oder kein Passwort enthalten.',
  insecure:
    'Ohne HTTPS funktioniert die Anmeldung nicht (das Sitzungs-Cookie ist nur über HTTPS gültig).',
  unreachable:
    'Unter dieser Adresse antwortet kein MowDeck-Server. Adresse, WLAN/VPN und Zertifikat prüfen.',
  useAnyway: 'Trotzdem verwenden',
  tryDemo: 'Demo ausprobieren',
  demoNote: 'Die Demo nutzt simulierte Mäher und wird regelmäßig zurückgesetzt.',
  about: 'Was ist MowDeck?',
  privacy: 'Datenschutz',
  moreInfo: 'Mehr Informationen',
  offlineTitle: 'Server nicht erreichbar',
  offlineLead: 'Die App erreicht deinen MowDeck-Server gerade nicht.',
  retry: 'Erneut versuchen',
  change: 'Anderen Server wählen',
  language: 'Sprache',
} as const;

const en: Record<keyof typeof de, string> = {
  title: 'Connect MowDeck',
  lead: 'Enter the address of your MowDeck server. The app loads its interface directly from there.',
  explain: 'MowDeck is self-hosted: the app needs your own MowDeck server.',
  label: 'Server address',
  hint: 'for example mowdeck.home.lan',
  connect: 'Connect',
  checking: 'Checking …',
  empty: 'Please enter an address.',
  invalid: 'This is not a valid address.',
  scheme: 'Only https:// or http:// is possible.',
  path: 'Only the address without a path, for example https://mowdeck.home.lan',
  credentials: 'The address must not contain a user name or password.',
  insecure: 'Login needs HTTPS (the session cookie is only valid over HTTPS).',
  unreachable:
    'No MowDeck server answers at this address. Check the address, Wi-Fi/VPN and certificate.',
  useAnyway: 'Use anyway',
  tryDemo: 'Try the demo',
  demoNote: 'The demo uses simulated mowers and is reset regularly.',
  about: 'What is MowDeck?',
  privacy: 'Privacy',
  moreInfo: 'More information',
  offlineTitle: 'Server not reachable',
  offlineLead: 'The app cannot reach your MowDeck server right now.',
  retry: 'Try again',
  change: 'Choose another server',
  language: 'Language',
};

export type TextKey = keyof typeof de;

export type Language = 'de' | 'en';

export const LANGUAGE_KEY = 'mowdeck.language';

type LanguageStorage = Pick<Storage, 'getItem' | 'setItem'>;

function isLanguage(value: unknown): value is Language {
  return value === 'de' || value === 'en';
}

/** The stored choice wins over the system language. */
export function resolveLanguage(
  stored: string | null | undefined,
  system: string | undefined,
): Language {
  if (isLanguage(stored)) return stored;
  return system?.toLowerCase().startsWith('de') ? 'de' : 'en';
}

export function readLanguage(storage: LanguageStorage | null): string | null {
  try {
    return storage?.getItem(LANGUAGE_KEY) ?? null;
  } catch {
    return null;
  }
}

export function storeLanguage(storage: LanguageStorage | null, language: Language): void {
  try {
    storage?.setItem(LANGUAGE_KEY, language);
  } catch {
    // the choice then lasts until the page closes
  }
}

export function texts(language: string | undefined): Record<TextKey, string> {
  return language?.toLowerCase().startsWith('de') ? de : en;
}

/** Fill every `[data-t]` element with its text and set the document language. */
export function applyTexts(root: Document, t: Record<TextKey, string>, language: string): void {
  root.documentElement.lang = language.toLowerCase().startsWith('de') ? 'de' : 'en';
  for (const element of root.querySelectorAll<HTMLElement>('[data-t]')) {
    const key = element.dataset.t as TextKey;
    if (key in t) element.textContent = t[key];
  }
  for (const element of root.querySelectorAll<HTMLInputElement>('[data-t-placeholder]')) {
    const key = element.dataset.tPlaceholder as TextKey;
    if (key in t) element.placeholder = t[key];
  }
  for (const element of root.querySelectorAll<HTMLElement>('[data-t-label]')) {
    const key = element.dataset.tLabel as TextKey;
    if (key in t) element.setAttribute('aria-label', t[key]);
  }
}

export interface PageLanguage {
  /** The texts of the language in use now (read it when a message is shown). */
  texts: () => Record<TextKey, string>;
  language: () => Language;
}

/**
 * Set a bundled page's language up: apply the stored or system language, and make the
 * `[data-language]` buttons switch it at once and remember the choice.
 */
export function startLanguage(
  root: Document,
  storage: LanguageStorage | null,
  system: string | undefined,
): PageLanguage {
  let current = resolveLanguage(readLanguage(storage), system);
  const buttons = [...root.querySelectorAll<HTMLElement>('[data-language]')];
  const show = (): void => {
    applyTexts(root, texts(current), current);
    for (const button of buttons) {
      button.setAttribute('aria-pressed', String(button.dataset.language === current));
    }
  };
  for (const button of buttons) {
    button.addEventListener('click', () => {
      const choice = button.dataset.language;
      if (!isLanguage(choice) || choice === current) return;
      current = choice;
      storeLanguage(storage, current);
      show();
    });
  }
  show();
  return { texts: () => texts(current), language: () => current };
}

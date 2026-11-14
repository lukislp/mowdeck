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
    'Der Server ist von hier aus gerade nicht erreichbar. Adresse, WLAN/VPN und Zertifikat prüfen.',
  notMowDeck:
    'Diese Adresse antwortet, ist aber kein MowDeck-Server. Die App öffnet nur MowDeck-Server. Adresse prüfen, oder zuerst die Anmeldeseite des WLANs bestätigen.',
  redirected: 'Diese Adresse leitet zu {host} weiter. Fahre nur fort, wenn das dein Server ist.',
  redirectedOpen: 'Mit {host} verbinden',
  useAnyway: 'Trotzdem verwenden',
  anywayTitle: '{host} ohne Prüfung öffnen?',
  anywayBody:
    'Die App kann {host} gerade nicht prüfen. Fahre nur fort, wenn das dein eigener MowDeck-Server ist, zum Beispiel weil du nicht im Heimnetz bist.',
  anywayOpen: '{host} öffnen',
  cancel: 'Abbrechen',
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
  aboutApp: 'Über die App und Lizenzen',
  aboutTitle: 'Über die App',
  back: 'Zurück',
  aboutVersion: 'Version',
  aboutBuild: 'Build',
  aboutVersionUnknown: 'nur in der installierten App',
  aboutLicenceTitle: 'Lizenz',
  aboutLicence:
    'Der Code dieser App steht unter der GNU AGPL-3.0 (nur Version 3), die gemeinsamen Gestaltungs-Stile (@mowdeck/ui) unter Apache-2.0. Lizenz und Quellcode-Angebot deines MowDeck-Servers findest du im Bereich „Über“ der Server-Oberfläche.',
  aboutSourceTitle: 'Quellcode',
  aboutSourceVersion: 'Der Quellcode genau dieser Version:',
  aboutSourceProject: 'Der Quellcode des Projekts:',
  aboutAffiliation:
    'MowDeck ist ein unabhängiges Projekt und steht in keiner Verbindung zu den Herstellern der unterstützten Mähroboter.',
  aboutNoticesTitle: 'Software Dritter',
  aboutNoticesLead:
    'Diese App enthält die folgenden Open-Source-Komponenten. Hier stehen ihre Urheberrechtshinweise und Lizenztexte.',
  aboutNoticesLoading: 'Hinweise werden geladen …',
  aboutNoticesError: 'Die Hinweise konnten nicht geladen werden.',
  aboutLicenceText: 'Lizenztext',
  aboutPrivacyTitle: 'Datenschutz',
  aboutPrivacyPolicy: 'Datenschutzerklärung',
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
    'The server cannot be reached from here right now. Check the address, Wi-Fi/VPN and certificate.',
  notMowDeck:
    'This address answers, but it is not a MowDeck server. The app only opens MowDeck servers. Check the address, or confirm the Wi-Fi sign-in page first.',
  redirected: 'This address redirects to {host}. Only continue if that is your server.',
  redirectedOpen: 'Connect to {host}',
  useAnyway: 'Use anyway',
  anywayTitle: 'Open {host} without checking?',
  anywayBody:
    'The app cannot check {host} right now. Only continue if it is your own MowDeck server, for example because you are not on your home network.',
  anywayOpen: 'Open {host}',
  cancel: 'Cancel',
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
  aboutApp: 'About and licences',
  aboutTitle: 'About the app',
  back: 'Back',
  aboutVersion: 'Version',
  aboutBuild: 'Build',
  aboutVersionUnknown: 'shown in the installed app only',
  aboutLicenceTitle: 'Licence',
  aboutLicence:
    'The code of this app is licensed under the GNU AGPL-3.0 (version 3 only), the shared design styles (@mowdeck/ui) under Apache-2.0. The licence and the source offer of your MowDeck server are in the About section of the server’s own interface.',
  aboutSourceTitle: 'Source code',
  aboutSourceVersion: 'The source code of exactly this version:',
  aboutSourceProject: 'The source code of the project:',
  aboutAffiliation:
    'MowDeck is an independent project and is not affiliated with the manufacturers of the supported mowers.',
  aboutNoticesTitle: 'Third-party software',
  aboutNoticesLead:
    'This app contains the following open-source components. Their copyright notices and licence texts are listed here.',
  aboutNoticesLoading: 'Loading notices …',
  aboutNoticesError: 'The notices could not be loaded.',
  aboutLicenceText: 'Licence text',
  aboutPrivacyTitle: 'Privacy',
  aboutPrivacyPolicy: 'Privacy policy',
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

/** Put values into a text's `{name}` placeholders. */
export function fill(text: string, values: Record<string, string>): string {
  return text.replace(/\{(\w+)\}/g, (match, name: string) => values[name] ?? match);
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
 * `[data-language]` buttons switch it at once and remember the choice. `onShow` runs after
 * every application of the texts, for what a page builds from script (the About page's notices).
 */
export function startLanguage(
  root: Document,
  storage: LanguageStorage | null,
  system: string | undefined,
  onShow?: (t: Record<TextKey, string>, language: Language) => void,
): PageLanguage {
  let current = resolveLanguage(readLanguage(storage), system);
  const buttons = [...root.querySelectorAll<HTMLElement>('[data-language]')];
  const show = (): void => {
    applyTexts(root, texts(current), current);
    for (const button of buttons) {
      button.setAttribute('aria-pressed', String(button.dataset.language === current));
    }
    onShow?.(texts(current), current);
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

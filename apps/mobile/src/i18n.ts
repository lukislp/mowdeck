/** Texts of the bundled pages (German or English, after the system language). */

const de = {
  title: 'MowDeck verbinden',
  lead: 'Gib die Adresse deines MowDeck-Servers ein. Die App lädt die Oberfläche direkt von dort.',
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
  offlineTitle: 'Server nicht erreichbar',
  offlineLead: 'Die App erreicht deinen MowDeck-Server gerade nicht.',
  retry: 'Erneut versuchen',
  change: 'Anderen Server wählen',
} as const;

const en: Record<keyof typeof de, string> = {
  title: 'Connect MowDeck',
  lead: 'Enter the address of your MowDeck server. The app loads its interface directly from there.',
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
  offlineTitle: 'Server not reachable',
  offlineLead: 'The app cannot reach your MowDeck server right now.',
  retry: 'Try again',
  change: 'Choose another server',
};

export type TextKey = keyof typeof de;

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
}

/**
 * "Was ist neu": the notes of `/whats-new.json` (built at release time by
 * build-tools/whats-new.mjs, shipped in the web image), which of them a device has not seen yet,
 * and the text in the user's language. Everything read from the file is untrusted and validated.
 */

export type Locale = 'de' | 'en';

export interface WhatsNewEntry {
  id: string;
  kind: 'feature' | 'fix';
  en: string;
  /** Curated German text; absent for an entry that only has its English commit subject. */
  de: string | null;
  /** A path of the app ("Jetzt ausprobieren"), never an external address. */
  href: string | null;
}

export interface WhatsNewRelease {
  version: string;
  date: string;
  entries: WhatsNewEntry[];
}

const VERSION = /^(\d+)\.(\d+)\.(\d+)$/;
const PATH = /^\/(?!\/)[^\s"'<>\\]*$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;

/** `1.2.3` or `v1.2.3` as numbers; null for anything else (a test build, a dev build). */
export function parseVersion(value: string | null | undefined): [number, number, number] | null {
  const match = VERSION.exec((value ?? '').trim().replace(/^v/, ''));
  return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : null;
}

/** Negative when a is older than b, 0 when equal, positive when newer; null if either is no version. */
export function compareVersions(a: string, b: string): number | null {
  const x = parseVersion(a);
  const y = parseVersion(b);
  if (!x || !y) return null;
  return x[0] - y[0] || x[1] - y[1] || x[2] - y[2];
}

function text(value: unknown, max = 240): string | null {
  return typeof value === 'string' && value.trim() && value.length <= max ? value.trim() : null;
}

function parseEntry(raw: unknown): WhatsNewEntry | null {
  if (!raw || typeof raw !== 'object') return null;
  const { id, kind, en, de, href } = raw as Record<string, unknown>;
  const english = text(en);
  const identifier = text(id, 64);
  if (!english || !identifier) return null;
  return {
    id: identifier,
    kind: kind === 'fix' ? 'fix' : 'feature',
    en: english,
    de: text(de),
    href: typeof href === 'string' && PATH.test(href) ? href : null,
  };
}

/** Validate the untrusted file; anything invalid is dropped, never thrown. Newest first. */
export function parseWhatsNew(raw: unknown): WhatsNewRelease[] {
  if (!raw || typeof raw !== 'object') return [];
  const releases = (raw as { releases?: unknown }).releases;
  if (!Array.isArray(releases)) return [];
  const out: WhatsNewRelease[] = [];
  for (const item of releases) {
    if (!item || typeof item !== 'object') continue;
    const { version, date, entries } = item as Record<string, unknown>;
    if (typeof version !== 'string' || !parseVersion(version) || !Array.isArray(entries)) continue;
    const parsed = entries
      .map(parseEntry)
      .filter((entry): entry is WhatsNewEntry => entry !== null);
    if (!parsed.length) continue;
    out.push({
      version: version.replace(/^v/, ''),
      date: typeof date === 'string' && DATE.test(date) ? date : '',
      entries: parsed,
    });
  }
  return out.sort((a, b) => compareVersions(b.version, a.version) ?? 0);
}

/**
 * The releases a device has not seen: newer than `seen` and not newer than the running `current`
 * version (the file of a build may already know a later tag, never the other way round). Without
 * a `seen` version (first start of this device) all of them count and the sheet caps the list. Null `current`
 * (a development or test build) shows nothing.
 */
export function unseenReleases(
  releases: readonly WhatsNewRelease[],
  seen: string | null,
  current: string | null,
): WhatsNewRelease[] {
  if (!current || !parseVersion(current)) return [];
  const upToNow = releases.filter(
    (release) => (compareVersions(release.version, current) ?? 1) <= 0,
  );
  if (!seen || !parseVersion(seen)) return upToNow;
  return upToNow.filter((release) => (compareVersions(release.version, seen) ?? -1) > 0);
}

/** The German text if the entry has one and the language is German; otherwise the English one. */
export function entryText(entry: WhatsNewEntry, locale: string): string {
  return locale.toLowerCase().startsWith('de') && entry.de ? entry.de : entry.en;
}

/** At most `limit` entries across the releases, newest release first; the rest is counted. */
export function limitEntries(
  releases: readonly WhatsNewRelease[],
  limit: number,
): { releases: WhatsNewRelease[]; hidden: number } {
  let left = limit;
  let hidden = 0;
  const shown: WhatsNewRelease[] = [];
  for (const release of releases) {
    const take = release.entries.slice(0, Math.max(left, 0));
    hidden += release.entries.length - take.length;
    left -= take.length;
    if (take.length) shown.push({ ...release, entries: take });
  }
  return { releases: shown, hidden };
}

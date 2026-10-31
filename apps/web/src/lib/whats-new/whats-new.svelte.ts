/**
 * State of "Was ist neu": the notes of the running build, the version this device last saw
 * (`localStorage`, key `mowdeck.whats-new-seen`) and whether the sheet is open. The version
 * comes from `/config.json` (`source.version`, the same value Settings → About shows), so it
 * works in the browser, the installed web app and the native shells, which have no service
 * worker. A device that cannot store the flag shows the sheet once per page load. When the
 * config says the image ships no notes (`whatsNew: false`, a build that is not a release) the
 * file is not requested at all.
 */
import { loadConfig } from '#lib/config.js';

import {
  compareVersions,
  parseVersion,
  parseWhatsNew,
  unseenReleases,
  type WhatsNewRelease,
} from './model.js';

export const SEEN_KEY = 'mowdeck.whats-new-seen';

function readSeen(): string | null {
  try {
    return localStorage.getItem(SEEN_KEY);
  } catch {
    return null;
  }
}

function writeSeen(version: string): void {
  try {
    localStorage.setItem(SEEN_KEY, version);
  } catch {
    // storage blocked: the sheet may return on the next page load
  }
}

/** Fetch `/whats-new.json`; null when the build ships none (a development build). */
export async function fetchWhatsNew(
  fetchFn: typeof fetch = (input, init) => fetch(input, init),
): Promise<WhatsNewRelease[] | null> {
  try {
    const response = await fetchFn('/whats-new.json', { cache: 'no-cache', credentials: 'omit' });
    if (!response.ok) return null;
    if (!(response.headers.get('content-type') ?? '').includes('json')) return null;
    return parseWhatsNew(await response.json());
  } catch {
    return null;
  }
}

class WhatsNew {
  /** All notes of the build, newest first, never newer than the running version. */
  history = $state<WhatsNewRelease[]>([]);
  /** The notes this device has not seen. */
  unseen = $state<WhatsNewRelease[]>([]);
  current = $state<string | null>(null);
  open = $state(false);
  loaded = $state(false);
  #started = false;

  async load(): Promise<void> {
    if (this.#started) return;
    this.#started = true;
    const config = await loadConfig();
    const releases = config.whatsNew === false ? null : await fetchWhatsNew();
    const version = parseVersion(config.source.version)?.join('.') ?? null;
    this.current = version;
    this.loaded = true;
    if (releases === null) return;
    this.history = version
      ? releases.filter((release) => (compareVersions(release.version, version) ?? 1) <= 0)
      : releases;
    this.unseen = unseenReleases(releases, readSeen(), version);
    if (this.unseen.length) this.open = true;
    else this.#remember();
  }

  /** Close the sheet; this device has now seen the running version. */
  dismiss(): void {
    this.open = false;
    this.#remember();
  }

  #remember(): void {
    const version = this.current;
    if (!version) return;
    const seen = readSeen();
    if (seen === null || !parseVersion(seen) || (compareVersions(version, seen) ?? 0) > 0)
      writeSeen(version);
  }
}

export const whatsNew = new WhatsNew();

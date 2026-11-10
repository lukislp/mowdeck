// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it, vi } from 'vitest';

import {
  LANGUAGE_KEY,
  readLanguage,
  resolveLanguage,
  startLanguage,
  storeLanguage,
  texts,
} from './i18n';

function memory(initial: Record<string, string> = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => void data.set(key, value),
  };
}

/** The little of the DOM the page language touches. */
function fakePage() {
  const title = { dataset: { t: 'title' }, textContent: '' } as unknown as HTMLElement;
  const group = {
    dataset: { tLabel: 'language' },
    label: '',
    setAttribute(name: string, value: string) {
      if (name === 'aria-label') this.label = value;
    },
  };
  const button = (language: string) => {
    const handlers: (() => void)[] = [];
    return {
      dataset: { language },
      pressed: '',
      setAttribute(name: string, value: string) {
        if (name === 'aria-pressed') this.pressed = value;
      },
      addEventListener: (_: string, handler: () => void) => handlers.push(handler),
      click: () => handlers.forEach((handler) => handler()),
    };
  };
  const de = button('de');
  const en = button('en');
  const root = {
    documentElement: { lang: '' },
    querySelectorAll(selector: string) {
      if (selector === '[data-t]') return [title];
      if (selector === '[data-language]') return [de, en];
      if (selector === '[data-t-label]') return [group];
      return [];
    },
  } as unknown as Document;
  return { root, title, group, de, en };
}

describe('resolveLanguage', () => {
  it('lets a stored choice win over the system language', () => {
    expect(resolveLanguage('en', 'de-DE')).toBe('en');
    expect(resolveLanguage('de', 'en-US')).toBe('de');
  });

  it('falls back to the system language and ignores junk', () => {
    expect(resolveLanguage(null, 'de-AT')).toBe('de');
    expect(resolveLanguage('fr', 'en-GB')).toBe('en');
    expect(resolveLanguage(undefined, undefined)).toBe('en');
  });
});

describe('language storage', () => {
  it('reads and writes the choice, and survives a blocked storage', () => {
    const storage = memory();
    storeLanguage(storage, 'de');
    expect(readLanguage(storage)).toBe('de');
    const blocked = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
    };
    expect(readLanguage(blocked)).toBeNull();
    expect(() => storeLanguage(blocked, 'en')).not.toThrow();
    expect(readLanguage(null)).toBeNull();
  });
});

describe('startLanguage', () => {
  it('applies the stored choice instead of the system language', () => {
    const page = fakePage();
    const language = startLanguage(page.root, memory({ [LANGUAGE_KEY]: 'en' }), 'de-DE');
    expect(language.language()).toBe('en');
    expect(page.title.textContent).toBe('Connect MowDeck');
    expect(page.root.documentElement.lang).toBe('en');
    expect(page.en.pressed).toBe('true');
    expect(page.de.pressed).toBe('false');
    expect(page.group.label).toBe('Language');
  });

  it('re-applies the texts at once and persists the toggle', () => {
    const page = fakePage();
    const storage = memory();
    const setItem = vi.spyOn(storage, 'setItem');
    const language = startLanguage(page.root, storage, 'en-US');
    expect(page.title.textContent).toBe('Connect MowDeck');
    page.de.click();
    expect(page.title.textContent).toBe('MowDeck verbinden');
    expect(page.root.documentElement.lang).toBe('de');
    expect(page.group.label).toBe('Sprache');
    expect(language.texts().retry).toBe('Erneut versuchen');
    expect(page.de.pressed).toBe('true');
    expect(setItem).toHaveBeenCalledWith(LANGUAGE_KEY, 'de');
    expect(startLanguage(fakePage().root, storage, 'en-US').language()).toBe('de');
  });

  it('does not store again when the active language is chosen', () => {
    const page = fakePage();
    const storage = memory();
    const setItem = vi.spyOn(storage, 'setItem');
    startLanguage(page.root, storage, 'en');
    page.en.click();
    expect(setItem).not.toHaveBeenCalled();
  });
});

describe('the texts of the bundled pages', () => {
  it('have a German and an English text for every key the pages use', () => {
    const de = texts('de');
    const en = texts('en');
    for (const file of ['index.html', 'offline.html', 'about.html']) {
      const html = readFileSync(resolve(import.meta.dirname, '..', file), 'utf8');
      const keys = [...html.matchAll(/data-t(?:-placeholder|-label)?="(\w+)"/g)].map((m) => m[1]!);
      expect(keys.length).toBeGreaterThan(0);
      for (const key of keys) {
        expect(de, `${file}: ${key} (de)`).toHaveProperty(key);
        expect(en, `${file}: ${key} (en)`).toHaveProperty(key);
      }
    }
  });
});

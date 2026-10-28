// @vitest-environment node
/**
 * The drive view's chips and labels stay readable in both themes.
 *
 * - The HUD plates over the camera (ADR 0044) are the same in light and dark because the video is
 *   never themed; anything written on them, or on a plate filled with `--md-hud-ink` (the pressed
 *   "both cameras" chip), needs an ink that is not themed either. Regression: the pressed chip used
 *   `--md-color-ink`, which turns light in the dark theme, so it loaded as a blank white button;
 *   the camera light's "on" word used `--md-state-paused`, a dark amber in the light theme, on the
 *   dark glass.
 * - A label on a solid state fill (the "blades on" chip on `--md-state-paused`, the "held" banner
 *   on `--md-state-error`) flips with the fill: both are themed, so the pair must hold in each
 *   theme. Regression: the blades-on chip wrote `--md-color-ink` on amber, light on light amber in
 *   the dark theme and dark on dark amber in the light one.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

// Vitest runs in apps/web.
const tokensCss = readFileSync(
  join(process.cwd(), '..', '..', 'libs', 'mowdeck-ui', 'src', 'lib', 'styles', 'tokens.css'),
  'utf8',
);
const sources = {
  drive: join('src', 'routes', '(app)', 'mowers', '[id]', 'drive', '+page.svelte'),
  stage: join('src', 'lib', 'components', 'CameraStage.svelte'),
  light: join('src', 'lib', 'components', 'LightChip.svelte'),
} as const;

type Source = keyof typeof sources;

/** The body of the block that starts at `open` (the index of its `{`), braces balanced. */
function blockAt(css: string, open: number): string {
  let depth = 0;
  for (let i = open; i < css.length; i += 1) {
    if (css[i] === '{') depth += 1;
    else if (css[i] === '}') {
      depth -= 1;
      if (depth === 0) return css.slice(open + 1, i);
    }
  }
  throw new Error('unbalanced block');
}

function blockAfter(css: string, selector: string): string {
  const at = css.indexOf(selector);
  if (at < 0) throw new Error(`no block for ${selector}`);
  return blockAt(css, css.indexOf('{', at));
}

function declarations(block: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const match of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    out.set(match[1]!, match[2]!.trim());
  }
  return out;
}

/** The first `:root { ... }` block: the light theme and every theme-independent token. */
const base = declarations(blockAt(tokensCss, tokensCss.indexOf('{', tokensCss.indexOf(':root'))));
const darkOverrides = declarations(blockAfter(tokensCss, ":root[data-theme='dark']"));
/** Tokens the dark theme redefines (both the media query and the explicit choice). */
const themed = new Set([
  ...declarations(blockAfter(tokensCss, '@media (prefers-color-scheme: dark)')).keys(),
  ...darkOverrides.keys(),
]);
const themes = { light: base, dark: new Map([...base, ...darkOverrides]) } as const;

/** A token's value in a theme, `var()` references followed. */
function resolve(theme: Map<string, string>, token: string): string {
  let value = theme.get(token);
  for (let i = 0; value?.startsWith('var(') && i < 5; i += 1) {
    value = theme.get(value.slice(4, -1).trim());
  }
  if (value === undefined) throw new Error(`unknown token ${token}`);
  return value;
}

/** Six hex digits of an opaque colour, or null (translucent, gradient, keyword). */
function hexDigits(value: string): string | null {
  const short = /^#([0-9a-f]{3})$/i.exec(value)?.[1];
  if (short) return [...short].map((c) => c + c).join('');
  return /^#([0-9a-f]{6})$/i.exec(value)?.[1] ?? null;
}

function luminance(hex: string): number {
  const value = hexDigits(hex);
  if (!value) throw new Error(`not an opaque hex colour: ${hex}`);
  const channel = (offset: number): number => {
    const c = parseInt(value.slice(offset, offset + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(0) + 0.7152 * channel(2) + 0.0722 * channel(4);
}

function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

interface Rule {
  file: Source;
  selector: string;
  /** The token of a `background: var(--x)` that is the whole value, else null. */
  background: string | null;
  /** The token of `color: var(--x)`, else null. */
  color: string | null;
}

/** Every rule of a component's style block (comments removed, at-rules unwrapped). */
function rulesOf(file: Source): Rule[] {
  const source = readFileSync(join(process.cwd(), sources[file]), 'utf8');
  // The CSS after the opening tag of the style block, without comments.
  const style = source
    .slice(source.indexOf('>', source.indexOf('<style')) + 1)
    .replace(/\/\*[\s\S]*?\*\//g, '');
  const rules: Rule[] = [];
  for (const match of style.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const body = match[2]!;
    rules.push({
      file,
      selector: match[1]!.trim(),
      background:
        /(?:^|[\s;])background(?:-color)?\s*:\s*var\((--[\w-]+)\)\s*;/.exec(body)?.[1] ?? null,
      color: /(?:^|[\s;])color\s*:\s*var\((--[\w-]+)\)/.exec(body)?.[1] ?? null,
    });
  }
  return rules;
}

const allRules = (Object.keys(sources) as Source[]).flatMap(rulesOf);

function rule(file: Source, selector: string): Rule {
  const found = allRules.find((r) => r.file === file && r.selector === selector);
  if (!found) throw new Error(`no rule ${selector} in ${file}`);
  return found;
}

describe('drive HUD chips over the camera', () => {
  it('keeps the HUD tokens the same in both themes', () => {
    for (const token of [
      '--md-hud-ink',
      '--md-hud-ink-inverse',
      '--md-hud-ink-muted',
      '--md-hud-glass',
      '--md-hud-lit',
    ]) {
      expect(base.has(token), token).toBe(true);
      expect(themed.has(token), token).toBe(false);
    }
    // The bugs: the theme ink is light in the dark theme, the paused amber dark in the light one.
    expect(themed.has('--md-color-ink')).toBe(true);
    expect(themed.has('--md-state-paused')).toBe(true);
  });

  it('gives the inverse HUD ink enough contrast on a lit plate', () => {
    expect(
      contrast(base.get('--md-hud-ink')!, base.get('--md-hud-ink-inverse')!),
    ).toBeGreaterThanOrEqual(7);
  });

  it('writes a lit chip (pressed "both cameras") with an unthemed, readable ink', () => {
    const lit = allRules.filter((r) => r.background === '--md-hud-ink');
    expect(lit.map((r) => r.selector)).toContain(".tool[aria-pressed='true']");
    for (const r of lit) {
      expect(r.color, r.selector).not.toBeNull();
      expect(themed.has(r.color!), `${r.selector}: ${r.color} is themed`).toBe(false);
      expect(
        contrast(base.get('--md-hud-ink')!, base.get(r.color!)!),
        r.selector,
      ).toBeGreaterThanOrEqual(4.5);
    }
  });

  it('writes the camera light chip with unthemed inks that read on the dark glass', () => {
    const inks = allRules.filter((r) => r.file === 'light' && r.color !== null);
    expect(inks.map((r) => r.selector)).toContain("[data-state='on'] .word");
    // The glass is a translucent --md-hud-ink-inverse; over a dark picture that is the plate.
    const plate = base.get('--md-hud-ink-inverse')!;
    for (const r of inks) {
      expect(themed.has(r.color!), `${r.selector}: ${r.color} is themed`).toBe(false);
      const ink = base.get(r.color!)!;
      if (hexDigits(ink)) {
        expect(contrast(ink, plate), r.selector).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it('writes the caption over the picture with an unthemed ink on an unthemed letterbox', () => {
    for (const selector of ['.caption', '.quiet-notice']) {
      const r = rule('stage', selector);
      expect(r.color, selector).not.toBeNull();
      expect(themed.has(r.color!), `${selector}: ${r.color} is themed`).toBe(false);
    }
    for (const selector of ['.stage', '.tile']) {
      const r = rule('stage', selector);
      expect(r.background, selector).not.toBeNull();
      expect(themed.has(r.background!), `${selector}: ${r.background} is themed`).toBe(false);
    }
  });
});

describe('labels on a solid fill', () => {
  /** Rules with an opaque token background and a token ink, in either theme. */
  const solid = allRules.filter(
    (r) =>
      r.background !== null &&
      r.color !== null &&
      Object.values(themes).every((theme) => hexDigits(resolve(theme, r.background!)) !== null),
  );

  it('covers the blades-on chip and the state banners', () => {
    const covered = solid.map((r) => `${r.file} ${r.selector}`);
    for (const expected of [
      'drive .blades-on',
      'drive .held',
      'stage .frozen strong',
      'stage .same',
      "stage .status[data-frozen='true']",
    ]) {
      expect(covered).toContain(expected);
    }
    expect(rule('drive', '.blades-on').background).toBe('--md-state-paused');
  });

  it.each(Object.entries(themes))('reads at 4.5:1 or better in the %s theme', (_name, theme) => {
    for (const r of solid) {
      const ratio = contrast(resolve(theme, r.color!), resolve(theme, r.background!));
      expect(
        ratio,
        `${r.file} ${r.selector}: ${r.color} on ${r.background}`,
      ).toBeGreaterThanOrEqual(4.5);
    }
  });
});

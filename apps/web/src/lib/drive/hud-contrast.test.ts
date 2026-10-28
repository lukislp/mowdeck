// @vitest-environment node
/**
 * The drive view's chips over the camera picture stay readable in both themes. The HUD plates
 * (ADR 0044) are the same in light and dark because the video is never themed; a chip filled with
 * `--md-hud-ink` (the pressed "both cameras" chip) therefore needs an ink that is not themed
 * either. Regression: the pressed chip used `--md-color-ink`, which turns light in the dark theme,
 * so with "both cameras" remembered as the layout it loaded as a blank white button.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

// Vitest runs in apps/web.
const tokensCss = readFileSync(
  join(process.cwd(), '..', '..', 'libs', 'mowdeck-ui', 'src', 'lib', 'styles', 'tokens.css'),
  'utf8',
);
const drivePage = readFileSync(
  join(process.cwd(), 'src', 'routes', '(app)', 'mowers', '[id]', 'drive', '+page.svelte'),
  'utf8',
);

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
/** Tokens the dark theme redefines (both the media query and the explicit choice). */
const themed = new Set([
  ...declarations(blockAfter(tokensCss, '@media (prefers-color-scheme: dark)')).keys(),
  ...declarations(blockAfter(tokensCss, ":root[data-theme='dark']")).keys(),
]);

function luminance(hex: string): number {
  const value = /^#([0-9a-f]{6})$/i.exec(hex)?.[1];
  if (!value) throw new Error(`not a 6-digit hex colour: ${hex}`);
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

/** Every rule of the page's style block that fills its plate with `--md-hud-ink`. */
function litRules(): { selector: string; color: string | null }[] {
  // The CSS after the opening tag of the style block, without comments.
  const style = drivePage
    .slice(drivePage.indexOf('>', drivePage.indexOf('<style')) + 1)
    .replace(/\/\*[\s\S]*?\*\//g, '');
  const rules: { selector: string; color: string | null }[] = [];
  for (const match of style.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const body = match[2]!;
    if (!/background(-color)?\s*:\s*var\(--md-hud-ink\)/.test(body)) continue;
    const color = /(?:^|[\s;])color\s*:\s*var\((--[\w-]+)\)/.exec(body)?.[1] ?? null;
    rules.push({ selector: match[1]!.trim(), color });
  }
  return rules;
}

describe('drive HUD chips over the camera', () => {
  it('keeps the HUD tokens the same in both themes', () => {
    for (const token of ['--md-hud-ink', '--md-hud-ink-inverse', '--md-hud-glass']) {
      expect(base.has(token), token).toBe(true);
      expect(themed.has(token), token).toBe(false);
    }
    // The bug: the theme ink is light in the dark theme.
    expect(themed.has('--md-color-ink')).toBe(true);
  });

  it('gives the inverse HUD ink enough contrast on a lit plate', () => {
    expect(
      contrast(base.get('--md-hud-ink')!, base.get('--md-hud-ink-inverse')!),
    ).toBeGreaterThanOrEqual(7);
  });

  it('writes a lit chip (pressed "both cameras") with an unthemed, readable ink', () => {
    const rules = litRules();
    expect(rules.map((rule) => rule.selector)).toContain(".tool[aria-pressed='true']");
    for (const rule of rules) {
      expect(rule.color, rule.selector).not.toBeNull();
      expect(themed.has(rule.color!), `${rule.selector}: ${rule.color} is themed`).toBe(false);
      expect(
        contrast(base.get('--md-hud-ink')!, base.get(rule.color!)!),
        rule.selector,
      ).toBeGreaterThanOrEqual(4.5);
    }
  });
});

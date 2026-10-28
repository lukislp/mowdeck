#!/usr/bin/env node
// Contrast check of the design tokens (WCAG 2.2 relative luminance). Reads tokens.css, resolves
// the light theme (first :root block) and the dark theme (:root[data-theme='dark']) and fails
// when a pair is below its target: text 4.5:1 (7:1 for primary ink in light, the outdoor
// theme), state marks and controls 3:1 (SC 1.4.11).
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, '../src/lib/styles/tokens.css'), 'utf8');

function block(selector) {
  const start = css.indexOf(selector);
  if (start < 0) throw new Error(`selector ${selector} not found`);
  const open = css.indexOf('{', start);
  const close = css.indexOf('}', open);
  return css.slice(open + 1, close);
}

function parse(text) {
  const values = {};
  for (const match of text.matchAll(/(--md-[a-z0-9-]+):\s*([^;]+);/g)) {
    values[match[1]] = match[2].trim();
  }
  return values;
}

const light = parse(block(':root {'));
const dark = { ...light, ...parse(block(":root[data-theme='dark'] {")) };

function resolve(theme, name) {
  let value = theme[name];
  for (let i = 0; value && value.startsWith('var(') && i < 5; i += 1) {
    value = theme[value.slice(4, -1).trim()];
  }
  if (!value || !value.startsWith('#')) throw new Error(`${name} is not a hex colour`);
  return value;
}

function luminance(hex) {
  const full =
    hex.length === 4
      ? hex
          .slice(1)
          .split('')
          .map((c) => c + c)
          .join('')
      : hex.slice(1);
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16) / 255);
  const lin = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function ratio(a, b) {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}

const STATES = [
  'mowing',
  'returning',
  'charging',
  'docked',
  'paused',
  'error',
  'offline',
  'unknown',
];

const pairs = [
  ['--md-color-ink', '--md-color-bg', 4.5, 7],
  ['--md-color-ink', '--md-color-surface', 4.5, 7],
  ['--md-color-ink', '--md-color-surface-sunken', 4.5, 7],
  ['--md-color-ink-muted', '--md-color-surface', 4.5],
  ['--md-color-ink-muted', '--md-color-surface-sunken', 4.5],
  ['--md-color-ink-muted', '--md-color-bg', 4.5],
  ['--md-color-ink-inverse', '--md-color-ink', 4.5],
  ['--md-color-line-strong', '--md-color-surface', 3],
  ['--md-color-focus', '--md-color-bg', 3],
  ['--md-stop-ink', '--md-stop-fill', 4.5],
  ['--md-stop-fill', '--md-stop-ring', 3],
  // Labels on a solid state fill (the drive view's "blades on" chip, the "held" banner).
  ['--md-color-ink-inverse', '--md-state-paused', 4.5],
  ['--md-color-ink-inverse', '--md-state-error', 4.5],
  // The HUD over the camera picture is the same in both themes.
  ['--md-hud-ink-inverse', '--md-hud-ink', 4.5],
  ['--md-hud-lit', '--md-hud-ink-inverse', 4.5],
  ...STATES.flatMap((s) => [
    [`--md-state-${s}`, `--md-state-${s}-bg`, 4.5],
    [`--md-state-${s}`, '--md-color-surface', 3],
  ]),
];

let failed = 0;
for (const [name, theme] of [
  ['light', light],
  ['dark', dark],
]) {
  for (const [fg, bg, target, lightTarget] of pairs) {
    const needed = name === 'light' && lightTarget ? lightTarget : target;
    const value = ratio(resolve(theme, fg), resolve(theme, bg));
    if (value < needed) {
      failed += 1;
      console.error(`${name}: ${fg} on ${bg} = ${value.toFixed(2)} < ${needed}`);
    }
  }
}
if (failed) {
  console.error(`${failed} contrast pair(s) below target`);
  process.exit(1);
}
console.log(`tokens: ${pairs.length * 2} contrast pairs meet their targets`);

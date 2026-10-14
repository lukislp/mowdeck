/** Names of the design tokens (CSS custom properties in tokens.css), for code that needs
 * computed values (for example map layers): read them with `tokenValue`. */
export const STATE_TOKENS = [
  '--md-state-mowing',
  '--md-state-returning',
  '--md-state-charging',
  '--md-state-docked',
  '--md-state-paused',
  '--md-state-error',
  '--md-state-offline',
  '--md-state-unknown',
] as const;

export type StateToken = (typeof STATE_TOKENS)[number];

export type ColorToken =
  | StateToken
  | '--md-color-bg'
  | '--md-color-surface'
  | '--md-color-surface-sunken'
  | '--md-color-line'
  | '--md-color-line-strong'
  | '--md-color-ink'
  | '--md-color-ink-muted'
  | '--md-color-focus';

/** The computed value of a token on an element (default: the document root). */
export function tokenValue(name: ColorToken, element?: Element): string {
  const target = element ?? document.documentElement;
  return getComputedStyle(target).getPropertyValue(name).trim();
}

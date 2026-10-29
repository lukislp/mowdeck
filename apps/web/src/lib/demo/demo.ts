/**
 * The public demo (ADR 0065) in the web app. The services enforce the demo; the app only labels
 * it, so nobody mistakes it for a real installation: the banner on every page (also inside the
 * iOS/Android app shells, which load these pages), "Demo" in front of every title, and
 * "Try the demo" instead of the sign-in forms. Nothing here can be dismissed.
 */

import { PROBLEM_PREFIX } from '#lib/api/problem.js';

/** Problem type of an action the demo refuses (403). */
export const DEMO_PROBLEM = `${PROBLEM_PREFIX}demo-mode`;

/** Class on `<html>` while the demo banner is shown (it reserves the banner's height). */
export const DEMO_ROOT_CLASS = 'md-demo';

/** `title` with `label` in front, once. */
export function demoTitle(title: string, label: string): string {
  const prefix = `${label} · `;
  if (!title) return label;
  return title.startsWith(prefix) ? title : `${prefix}${title}`;
}

/** Whether an API problem is the demo's refusal. */
export function isDemoProblem(problem: { type?: string } | null | undefined): boolean {
  return problem?.type === DEMO_PROBLEM;
}

/**
 * Keep `label` in front of the document title while pages change it (`<svelte:head>` replaces
 * the title element's text). Returns the function that stops it.
 */
export function keepDemoTitle(doc: Document, label: string): () => void {
  const apply = () => {
    const next = demoTitle(doc.title, label);
    if (doc.title !== next) doc.title = next;
  };
  apply();
  const observer = new MutationObserver(apply);
  observer.observe(doc.head, { subtree: true, childList: true, characterData: true });
  return () => observer.disconnect();
}

/**
 * A tiny Markdown subset for the legal pages (`static/legal/*.md`, written by the operator):
 * headings (`#` to `####`), paragraphs, unordered and ordered lists, `**bold**` and
 * `[text](url)` links. The result is a block tree that the page renders with plain Svelte text
 * nodes, so raw HTML in the file is shown as text and never interpreted. Links keep only
 * http(s), mailto and tel targets and paths on this origin.
 */

export type Inline =
  | { kind: 'text'; text: string }
  | { kind: 'strong'; text: string }
  | { kind: 'link'; text: string; href: string };

export type Block =
  | { kind: 'heading'; level: 1 | 2 | 3 | 4; content: Inline[] }
  | { kind: 'paragraph'; content: Inline[] }
  | { kind: 'list'; ordered: boolean; items: Inline[][] };

/** The legal pages the app knows; anything else is "not found". */
export const LEGAL_PAGES = ['imprint', 'terms', 'privacy'] as const;
export type LegalPage = (typeof LEGAL_PAGES)[number];

export function isLegalPage(value: string): value is LegalPage {
  return (LEGAL_PAGES as readonly string[]).includes(value);
}

/** A link target that is safe to render, or null (the link then shows as text). */
export function safeHref(raw: string): string | null {
  const href = raw.trim();
  if (/^(https?:|mailto:|tel:)/i.test(href)) return href;
  if (/^\/(?!\/)/.test(href) || href.startsWith('#')) return href;
  return null;
}

const INLINE = /\*\*(.+?)\*\*|\[([^\]]+)\]\(([^)\s]+)\)/g;

export function parseInline(text: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  const push = (part: string) => {
    if (!part) return;
    const prev = out[out.length - 1];
    if (prev?.kind === 'text') prev.text += part;
    else out.push({ kind: 'text', text: part });
  };
  for (const match of text.matchAll(INLINE)) {
    push(text.slice(last, match.index));
    last = match.index + match[0].length;
    if (match[1] !== undefined) {
      out.push({ kind: 'strong', text: match[1] });
      continue;
    }
    const label = match[2] ?? '';
    const href = safeHref(match[3] ?? '');
    if (href) out.push({ kind: 'link', text: label, href });
    else push(label);
  }
  push(text.slice(last));
  return out;
}

const HEADING = /^(#{1,4})\s+(.*)$/;
const BULLET = /^\s*[-*+]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;

export function parseMarkdown(source: string): Block[] {
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;

  const flush = () => {
    if (paragraph.length) {
      blocks.push({ kind: 'paragraph', content: parseInline(paragraph.join(' ')) });
      paragraph = [];
    }
    if (list) {
      blocks.push({ kind: 'list', ordered: list.ordered, items: list.items.map(parseInline) });
      list = null;
    }
  };

  for (const raw of source.replace(/\r\n?/g, '\n').split('\n')) {
    const line = raw.trimEnd();
    const heading = HEADING.exec(line);
    const bullet = BULLET.exec(line);
    const numbered = bullet ? null : NUMBERED.exec(line);
    const item = bullet ?? numbered;
    if (!line.trim()) {
      flush();
    } else if (heading) {
      flush();
      const level = (heading[1] ?? '#').length as 1 | 2 | 3 | 4;
      blocks.push({ kind: 'heading', level, content: parseInline((heading[2] ?? '').trim()) });
    } else if (item) {
      const ordered = !!numbered;
      const text = (item[1] ?? '').trim();
      if (paragraph.length || (list && list.ordered !== ordered)) flush();
      list ??= { ordered, items: [] };
      list.items.push(text);
    } else if (list && /^\s+\S/.test(raw)) {
      // An indented continuation line of the last list item.
      list.items[list.items.length - 1] += ` ${line.trim()}`;
    } else {
      if (list) flush();
      paragraph.push(line.trim());
    }
  }
  flush();
  return blocks;
}

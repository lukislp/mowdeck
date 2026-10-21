/**
 * Property-based tests (fast-check) of the legal Markdown subset: the operator's file is
 * rendered on public pages, so whatever it contains must parse without throwing and every link
 * that survives must be a web, mail or phone link or stay on this origin.
 */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { parseInline, parseMarkdown, safeHref } from './markdown';

const ORIGIN = 'https://mowdeck.example';
const ALLOWED_SCHEMES = new Set(['http:', 'https:', 'mailto:', 'tel:']);

/** Where a browser goes for `href` on a page of this origin. */
function target(href: string): URL {
  return new URL(href, `${ORIGIN}/legal/imprint`);
}

function staysSafe(href: string): boolean {
  let url: URL;
  try {
    url = target(href);
  } catch {
    return true; // not a URL: the browser does not follow it
  }
  if (href.startsWith('/') || href.startsWith('#')) return url.origin === ORIGIN;
  return ALLOWED_SCHEMES.has(url.protocol);
}

/** Link targets around the tricky spots: schemes, slashes and backslashes, whitespace. */
const linkTarget = fc.oneof(
  fc.string(),
  fc.webUrl(),
  fc
    .tuple(
      fc.constantFrom('/', '//', '/\\', '\\', '#', 'javascript:', 'JaVaScRiPt:', 'data:', ''),
      fc.constantFrom('', '\t', '\n', ' ', '\\', '/', '%2f', '%5c'),
      fc.domain(),
      fc.webPath(),
    )
    .map((parts) => parts.join('')),
);

const markdownLine = fc.oneof(
  fc.string(),
  fc.tuple(fc.string(), linkTarget).map(([text, href]) => `see [${text}](${href}) now`),
  fc.tuple(fc.constantFrom('#', '##', '-', '1.', '  '), fc.string()).map((p) => p.join(' ')),
);

describe('legal markdown properties', () => {
  it('accepts a link target only when a browser keeps it on an allowed target', () => {
    fc.assert(
      fc.property(linkTarget, (raw) => {
        const href = safeHref(raw);
        if (href !== null) expect(staysSafe(href)).toBe(true);
      }),
      { numRuns: 2000 },
    );
  });

  it('parses any document, and every rendered link is safe', () => {
    fc.assert(
      fc.property(fc.array(markdownLine, { maxLength: 30 }), (lines) => {
        const blocks = parseMarkdown(lines.join('\n'));
        const inlines = blocks.flatMap((block) =>
          block.kind === 'list' ? block.items.flat() : block.content,
        );
        for (const inline of inlines) {
          if (inline.kind === 'link') {
            expect(safeHref(inline.href)).toBe(inline.href);
            expect(staysSafe(inline.href)).toBe(true);
          }
        }
      }),
    );
  });

  it('keeps every character of plain text that has no markup', () => {
    fc.assert(
      fc.property(
        fc.string().filter((s) => !/[*[\]()]/.test(s)),
        (text) => {
          const out = parseInline(text);
          expect(out.map((part) => part.text).join('')).toBe(text);
        },
      ),
    );
  });
});

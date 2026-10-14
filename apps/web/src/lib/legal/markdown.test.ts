import { describe, expect, it } from 'vitest';

import { isLegalPage, parseInline, parseMarkdown, safeHref } from './markdown';

describe('legal markdown inline', () => {
  it('splits bold text and links from plain text', () => {
    expect(parseInline('a **b** [c](https://x.example/) d')).toEqual([
      { kind: 'text', text: 'a ' },
      { kind: 'strong', text: 'b' },
      { kind: 'text', text: ' ' },
      { kind: 'link', text: 'c', href: 'https://x.example/' },
      { kind: 'text', text: ' d' },
    ]);
  });

  it('keeps unsafe link targets as text', () => {
    expect(parseInline('see [here](javascript:void) now')).toEqual([
      { kind: 'text', text: 'see here now' },
    ]);
  });

  it('keeps HTML as text (it is never interpreted)', () => {
    expect(parseInline('<script>x</script>')).toEqual([
      { kind: 'text', text: '<script>x</script>' },
    ]);
  });

  it('allows web, mail, phone and same-origin targets only', () => {
    expect(safeHref('https://a.example')).toBe('https://a.example');
    expect(safeHref('mailto:me@example.org')).toBe('mailto:me@example.org');
    expect(safeHref('tel:+49301234')).toBe('tel:+49301234');
    expect(safeHref('/legal/imprint')).toBe('/legal/imprint');
    expect(safeHref('#top')).toBe('#top');
    expect(safeHref('//evil.example')).toBeNull();
    expect(safeHref('data:text/html,x')).toBeNull();
    expect(safeHref('relative/path')).toBeNull();
  });
});

describe('legal markdown blocks', () => {
  it('parses headings, paragraphs and lists', () => {
    const blocks = parseMarkdown(
      [
        '# Title',
        '',
        'First line',
        'continues here.',
        '',
        '## Lists',
        '- one',
        '* two **bold**',
        '  wrapped',
        '1. first',
        '2) second',
        'After the list.',
        '#### Small',
      ].join('\r\n'),
    );
    expect(blocks).toEqual([
      { kind: 'heading', level: 1, content: [{ kind: 'text', text: 'Title' }] },
      { kind: 'paragraph', content: [{ kind: 'text', text: 'First line continues here.' }] },
      { kind: 'heading', level: 2, content: [{ kind: 'text', text: 'Lists' }] },
      {
        kind: 'list',
        ordered: false,
        items: [
          [{ kind: 'text', text: 'one' }],
          [
            { kind: 'text', text: 'two ' },
            { kind: 'strong', text: 'bold' },
            { kind: 'text', text: ' wrapped' },
          ],
        ],
      },
      {
        kind: 'list',
        ordered: true,
        items: [[{ kind: 'text', text: 'first' }], [{ kind: 'text', text: 'second' }]],
      },
      { kind: 'paragraph', content: [{ kind: 'text', text: 'After the list.' }] },
      { kind: 'heading', level: 4, content: [{ kind: 'text', text: 'Small' }] },
    ]);
  });

  it('starts a list after a paragraph and treats deeper headings as text', () => {
    const blocks = parseMarkdown('Intro\n- item\n\n##### five');
    expect(blocks.map((b) => b.kind)).toEqual(['paragraph', 'list', 'paragraph']);
  });

  it('knows the three legal pages only', () => {
    expect(isLegalPage('imprint')).toBe(true);
    expect(isLegalPage('terms')).toBe(true);
    expect(isLegalPage('privacy')).toBe(true);
    expect(isLegalPage('cookies')).toBe(false);
  });
});

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { markdownCell } from './markdown-cell.mjs';

test('escapes pipes so a title cannot split the table row', () => {
  assert.equal(markdownCell('a | b'), 'a \\| b');
});

test('escapes backslashes before pipes and backticks', () => {
  assert.equal(markdownCell('a\\|b`c'), 'a\\\\\\|b\\`c');
});

test('turns line breaks into spaces', () => {
  assert.equal(markdownCell('a\nb\r\nc'), 'a b c');
});

test('leaves plain text alone', () => {
  assert.equal(markdownCell('opens the map > zoom'), 'opens the map > zoom');
});
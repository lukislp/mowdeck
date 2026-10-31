#!/usr/bin/env node
// Both message files have the same keys, no empty texts and the same {parameters} per key.
// A message is either a plain string or a list of variants (inlang message format, used for plural
// forms): then every variant text must be non-empty and the union of their parameters counts.
import { readFileSync } from 'node:fs';

const load = (locale) => JSON.parse(readFileSync(`messages/${locale}.json`, 'utf8'));
const de = load('de');
const en = load('en');
const keys = (messages) => Object.keys(messages).filter((k) => !k.startsWith('$'));
/** All texts of a message: the string itself, or every `match` text of its variants. */
const texts = (value) =>
  Array.isArray(value)
    ? value.flatMap((variant) => Object.values(variant?.match ?? {}).map(String))
    : [String(value)];
const params = (value) =>
  [...new Set(texts(value).flatMap((text) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1])))]
    .sort()
    .join(',');
const empty = (value) => {
  const all = texts(value);
  return all.length === 0 || all.some((text) => !text.trim());
};

const problems = [];
for (const key of keys(de)) if (!(key in en)) problems.push(`en.json misses ${key}`);
for (const key of keys(en)) if (!(key in de)) problems.push(`de.json misses ${key}`);
for (const key of keys(de)) {
  if (empty(de[key])) problems.push(`de.json: ${key} is empty`);
  if (key in en && empty(en[key])) problems.push(`en.json: ${key} is empty`);
  if (key in en && params(de[key]) !== params(en[key])) {
    problems.push(`${key}: parameters differ (${params(de[key])} / ${params(en[key])})`);
  }
}

if (problems.length) {
  console.error(problems.join('\n'));
  process.exit(1);
}
console.log(`i18n: ${keys(de).length} messages, de and en complete`);

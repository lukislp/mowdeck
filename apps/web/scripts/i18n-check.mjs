#!/usr/bin/env node
// Both message files have the same keys, no empty texts and the same {parameters} per key.
import { readFileSync } from 'node:fs';

const load = (locale) => JSON.parse(readFileSync(`messages/${locale}.json`, 'utf8'));
const de = load('de');
const en = load('en');
const keys = (messages) => Object.keys(messages).filter((k) => !k.startsWith('$'));
const params = (text) =>
  [...String(text).matchAll(/\{(\w+)\}/g)]
    .map((m) => m[1])
    .sort()
    .join(',');

const problems = [];
for (const key of keys(de)) if (!(key in en)) problems.push(`en.json misses ${key}`);
for (const key of keys(en)) if (!(key in de)) problems.push(`de.json misses ${key}`);
for (const key of keys(de)) {
  if (!String(de[key]).trim()) problems.push(`de.json: ${key} is empty`);
  if (key in en && !String(en[key]).trim()) problems.push(`en.json: ${key} is empty`);
  if (key in en && params(de[key]) !== params(en[key])) {
    problems.push(`${key}: parameters differ (${params(de[key])} / ${params(en[key])})`);
  }
}

if (problems.length) {
  console.error(problems.join('\n'));
  process.exit(1);
}
console.log(`i18n: ${keys(de).length} messages, de and en complete`);

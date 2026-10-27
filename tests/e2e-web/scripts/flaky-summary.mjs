/**
 * Markdown summary of a Playwright JSON report (merged-report/results.json) for the job summary:
 * totals, and the specs that failed first and passed on a retry ("flaky"), which do not fail the
 * job but should get fixed. Prints to stdout; usage: node scripts/flaky-summary.mjs <results.json>
 */
import { readFileSync } from 'node:fs';

const file = process.argv[2];
if (!file) {
  console.error('usage: node scripts/flaky-summary.mjs <results.json>');
  process.exit(2);
}

const report = JSON.parse(readFileSync(file, 'utf8'));
const counts = { expected: 0, unexpected: 0, flaky: 0, skipped: 0 };
const flaky = [];
const failed = [];

function walk(suite, trail) {
  const here = suite.title && !suite.file ? [...trail, suite.title] : trail;
  for (const spec of suite.specs ?? []) {
    for (const test of spec.tests ?? []) {
      counts[test.status] = (counts[test.status] ?? 0) + 1;
      const row = {
        where: `${spec.file}:${spec.line}`,
        title: [...here, spec.title].join(' > '),
        project: test.projectName,
        attempts: test.results?.length ?? 0,
      };
      if (test.status === 'flaky') flaky.push(row);
      if (test.status === 'unexpected') failed.push(row);
    }
  }
  for (const child of suite.suites ?? []) walk(child, here);
}
for (const suite of report.suites ?? []) walk(suite, []);

const cell = (text) => String(text).replaceAll('|', '\|');
const table = (rows) =>
  [
    '| Spec | Project | Attempts |',
    '| --- | --- | --- |',
    ...rows.map((r) => `| \`${cell(r.where)}\` ${cell(r.title)} | ${r.project} | ${r.attempts} |`),
  ].join('\n');

const lines = [
  '## e2e-web',
  '',
  `${counts.expected} passed, ${counts.flaky} flaky, ${counts.unexpected} failed, ${counts.skipped} skipped.`,
  '',
];
if (failed.length) lines.push('### Failed', '', table(failed), '');
if (flaky.length) {
  lines.push(
    '### Flaky (passed only on retry)',
    '',
    'These specs failed first and passed on a retry. The job stays green; please fix them.',
    '',
    table(flaky),
    '',
  );
  for (const r of flaky) {
    console.error(`::warning title=Flaky e2e-web spec::${r.where} ${r.title} (${r.project})`);
  }
}
console.log(lines.join('\n'));

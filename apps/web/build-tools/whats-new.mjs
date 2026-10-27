#!/usr/bin/env node
/**
 * Builds `static/whats-new.json`, the notes the web app shows after an update ("Was ist neu",
 * docs/dev/web-app.md). The release workflow runs it before the web image is built, from a
 * checkout with the tags; the file ships inside the image and is served as /whats-new.json.
 *
 *   node build-tools/whats-new.mjs --version 0.29.0 --out static/whats-new.json
 *
 * Source: the Conventional Commit subjects between the release tags (the same ones
 * semantic-release reads) plus the curated texts in `whats-new/*.json` (one file per change,
 * keyed by the pull request number that squash merge puts at the end of the subject).
 *
 *   * Only feat, fix and perf commits are user-facing. ci, build, chore, test, docs, style and
 *     refactor commits, the scopes ci/deps/build/release and Dependabot bumps are left out.
 *   * A commit with a curated entry shows the entry's German and English text and link; a
 *     commit without one shows its English subject (the app then shows it in both languages).
 *   * `hide: true` in a curated file keeps a user-facing commit out (a fix of an unreleased fix).
 *
 * Plain Node (no dependencies, no TypeScript): the release job has no pnpm install.
 */
import { execFileSync } from 'node:child_process';
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const USER_FACING = new Set(['feat', 'fix', 'perf']);
const HIDDEN_SCOPES = new Set(['ci', 'deps', 'deps-dev', 'build', 'release', 'docker']);
const SUBJECT = /^(\w+)(?:\(([^)]*)\))?(!)?: (.+)$/;
const TAG = /^v(\d+)\.(\d+)\.(\d+)$/;
/** The newest releases kept in the file; older notes stay in the GitHub release notes. */
export const MAX_RELEASES = 40;

/** @param {string} subject */
export function parseSubject(subject) {
  const match = SUBJECT.exec(subject.trim());
  if (!match) return null;
  const [, type, scope, bang, rest] = match;
  const pr = /\s\(#(\d+)\)$/.exec(rest);
  return {
    type: type.toLowerCase(),
    scope: scope ? scope.toLowerCase() : null,
    breaking: bang === '!',
    text: rest.replace(/\s\(#\d+\)$/, '').trim(),
    pr: pr ? Number(pr[1]) : null,
  };
}

/** True for a commit a user of the app notices. */
export function isUserFacing(parsed) {
  if (!parsed || !USER_FACING.has(parsed.type)) return false;
  if (parsed.scope && HIDDEN_SCOPES.has(parsed.scope)) return false;
  return !/^bump\s/i.test(parsed.text);
}

/** The English fallback text: the commit subject with a capital letter. */
export function subjectText(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function isPath(href) {
  return typeof href === 'string' && /^\/(?!\/)[^\s"'<>\\]*$/.test(href);
}

/**
 * Validate one curated file's content. Throws a message naming the file.
 * @param {unknown} raw
 * @param {string} file
 */
export function parseCurated(raw, file = 'entry') {
  const fail = (why) => {
    throw new Error(`whats-new/${file}: ${why}`);
  };
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) fail('must be a JSON object');
  const { pr, de, en, href, hide } = raw;
  const prs = (Array.isArray(pr) ? pr : [pr]).filter((n) => n !== undefined);
  if (!prs.length || !prs.every((n) => Number.isInteger(n) && n > 0)) {
    fail('"pr" must be a pull request number or a list of them');
  }
  if (hide === true) return { prs, hide: true };
  for (const [key, value] of [
    ['de', de],
    ['en', en],
  ]) {
    if (typeof value !== 'string' || !value.trim() || value.length > 240) {
      fail(`"${key}" must be a text of 1 to 240 characters`);
    }
  }
  if (href !== undefined && !isPath(href))
    fail('"href" must be a path of the app, e.g. "/settings#linked-accounts"');
  return { prs, hide: false, de: de.trim(), en: en.trim(), ...(href ? { href } : {}) };
}

/** Read every `*.json` of a directory of curated entries. */
export function loadCurated(dir) {
  return readdirSync(dir)
    .filter((name) => name.endsWith('.json'))
    .sort()
    .map((name) => parseCurated(JSON.parse(readFileSync(join(dir, name), 'utf8')), name));
}

/**
 * The releases of the file, newest first.
 * @param {{ version: string, date: string, commits: string[] }[]} releases oldest first,
 *   commits = subjects of the commits that release contains
 * @param {ReturnType<typeof parseCurated>[]} curated
 */
export function buildReleases(releases, curated, max = MAX_RELEASES) {
  const byPr = new Map();
  for (const entry of curated) for (const pr of entry.prs) byPr.set(pr, entry);
  const used = new Set();
  const out = [];
  // Newest first, so a curated entry that covers several pull requests (a feature and its fix)
  // lands once, in the newest release that contains one of them.
  for (const release of [...releases].reverse()) {
    const entries = [];
    for (const subject of release.commits) {
      const parsed = parseSubject(subject);
      if (!isUserFacing(parsed)) continue;
      const curatedEntry = parsed.pr === null ? undefined : byPr.get(parsed.pr);
      if (curatedEntry?.hide) continue;
      if (curatedEntry) {
        if (used.has(curatedEntry)) continue;
        used.add(curatedEntry);
        entries.push({
          id: `pr-${curatedEntry.prs[0]}`,
          kind: parsed.type === 'feat' ? 'feature' : 'fix',
          en: curatedEntry.en,
          de: curatedEntry.de,
          ...(curatedEntry.href ? { href: curatedEntry.href } : {}),
        });
      } else {
        entries.push({
          id: parsed.pr === null ? `c-${entries.length}` : `pr-${parsed.pr}`,
          kind: parsed.type === 'feat' ? 'feature' : 'fix',
          en: subjectText(parsed.text),
        });
      }
    }
    if (entries.length) out.push({ version: release.version, date: release.date, entries });
  }
  return out.slice(0, max);
}

function git(args, cwd) {
  return execFileSync('git', args, {
    cwd,
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
  }).trim();
}

/**
 * The releases from the repository's tags; a version without a tag (a release build runs after
 * the tag exists, a test build never has one) is added as the commits since the newest tag.
 */
export function readReleases(version, cwd = undefined) {
  const run = (args) => git(args, cwd);
  const tags = run(['tag', '--list', 'v*'])
    .split('\n')
    .filter((tag) => TAG.test(tag))
    .sort((a, b) => {
      const [x, y] = [a, b].map((tag) => TAG.exec(tag).slice(1).map(Number));
      return x[0] - y[0] || x[1] - y[1] || x[2] - y[2];
    });
  const rows = tags.map((tag, index) => ({
    version: tag.slice(1),
    date: run(['log', '-1', '--format=%cs', tag]),
    range: index === 0 ? tag : `${tags[index - 1]}..${tag}`,
  }));
  if (version && !tags.includes(`v${version}`)) {
    rows.push({
      version,
      date: run(['log', '-1', '--format=%cs', 'HEAD']),
      range: tags.length ? `${tags[tags.length - 1]}..HEAD` : 'HEAD',
    });
  }
  // The newest releases are enough; the first kept one needs its own range only.
  return rows.slice(-(MAX_RELEASES * 3)).map((row) => ({
    version: row.version,
    date: row.date,
    commits: run(['log', '--no-merges', '--format=%s', row.range]).split('\n').filter(Boolean),
  }));
}

function main(argv) {
  const arg = (name) => {
    const index = argv.indexOf(name);
    return index >= 0 ? argv[index + 1] : undefined;
  };
  const root = join(dirname(fileURLToPath(import.meta.url)), '..');
  const out = arg('--out') ?? join(root, 'static', 'whats-new.json');
  const version = arg('--version');
  if (version !== undefined && !/^\d+\.\d+\.\d+$/.test(version)) {
    // A test build (0.1.0-rc.1) is not a release line the app compares against.
    console.log(`whats-new: ${version} is no plain version; notes cover the tags only`);
  }
  const plain = version && /^\d+\.\d+\.\d+$/.test(version) ? version : undefined;
  const releases = buildReleases(readReleases(plain), loadCurated(join(root, 'whats-new')));
  mkdirSync(dirname(out), { recursive: true });
  writeFileSync(out, `${JSON.stringify({ format: 1, releases }, null, 2)}\n`);
  const count = releases.reduce((sum, release) => sum + release.entries.length, 0);
  console.log(`whats-new: ${count} entries in ${releases.length} releases -> ${out}`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1])
  main(process.argv.slice(2));

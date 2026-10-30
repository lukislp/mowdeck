#!/usr/bin/env node
/**
 * The API explorer's frame (ADR 0072): the files nginx serves under `/_explorer/`.
 *
 *   node build-tools/api-explorer.mjs build      # after `vite build` and csp-hashes.mjs
 *
 * * `scalar.js`: Scalar's standalone browser bundle from the pinned `@scalar/api-reference` in
 *   node_modules (package.json pins the exact version, Dependabot bumps it, the lockfile holds
 *   its integrity). Copied, never checked in, never loaded from a CDN.
 * * `index.html`, `explorer.js`, `frame.js`, `explorer.css`: the frame page and its glue code
 *   (`api-explorer/`); the page gets the SHA-384 integrity of its scripts and its stylesheet.
 * * `openapi/<service>.json` and `openapi/index.json`: the services' OpenAPI documents
 *   (`apps/web/openapi`, exported by `uv run poe openapi`), every one with operations under
 *   `/api/v1`, Fleet first.
 * * `LICENSE.txt`: Scalar's licence (MIT) and version.
 *
 * The frame is written after the SPA's CSP is computed, so none of its files can widen the app's
 * policy, and outside SvelteKit's build, so the service worker never precaches it. nginx serves it
 * only with `MOWDECK_WEB_API_EXPLORER=true` (nginx/40-mowdeck-runtime-config.sh); `pnpm dev`
 * serves it through the Vite plugin below.
 *
 * Plain Node (no dependencies, no TypeScript), like whats-new.mjs.
 */
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/** URL path of the frame. */
export const EXPLORER_BASE = '/_explorer/';
/** The npm package of the viewer. */
export const SCALAR_PACKAGE = '@scalar/api-reference';
/** Services listed first; the others follow alphabetically. */
const FIRST = ['fleet', 'telemetry', 'scheduler', 'notifications', 'planner', 'vision'];
const GLUE = ['explorer.js', 'frame.js', 'explorer.css'];

const WEB_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

/** `'sha384-…'` of a file's bytes, for an `integrity` attribute. */
export function integrity(bytes) {
  return `sha384-${createHash('sha384').update(bytes).digest('base64')}`;
}

/** Root directory and version of the installed Scalar package. */
export function scalarPackage(webRoot = WEB_ROOT) {
  const require = createRequire(join(webRoot, 'package.json'));
  let dir = dirname(require.resolve(SCALAR_PACKAGE));
  while (
    !existsSync(join(dir, 'package.json')) ||
    readJson(join(dir, 'package.json')).name !== SCALAR_PACKAGE
  ) {
    const up = dirname(dir);
    if (up === dir) throw new Error(`${SCALAR_PACKAGE} not found from ${webRoot}`);
    dir = up;
  }
  const manifest = readJson(join(dir, 'package.json'));
  return { dir, version: String(manifest.version), license: String(manifest.license) };
}

/** The exact version web/package.json pins (no range: the build refuses one). */
export function pinnedVersion(webRoot = WEB_ROOT) {
  const manifest = readJson(join(webRoot, 'package.json'));
  const spec =
    manifest.devDependencies?.[SCALAR_PACKAGE] ?? manifest.dependencies?.[SCALAR_PACKAGE];
  if (typeof spec !== 'string' || !/^\d+\.\d+\.\d+$/.test(spec)) {
    throw new Error(`${SCALAR_PACKAGE} must be pinned to an exact version in package.json`);
  }
  return spec;
}

/**
 * The OpenAPI documents to show, `{ slug, title, document }`, in display order: every service
 * with at least one operation under `/api/v1` (Realtime has only its WebSocket).
 */
export function explorerDocuments(openapiDir) {
  const docs = readdirSync(openapiDir)
    .filter((name) => /^[a-z][a-z-]*\.json$/.test(name))
    .map((name) => {
      const slug = name.replace(/\.json$/, '');
      const document = readJson(join(openapiDir, name));
      return { slug, title: titleOf(slug), document };
    })
    .filter(({ document }) =>
      Object.keys(document.paths ?? {}).some((p) => p.startsWith('/api/v1/')),
    );
  const rank = (slug) => {
    const at = FIRST.indexOf(slug);
    return at < 0 ? FIRST.length : at;
  };
  return docs.sort((a, b) => rank(a.slug) - rank(b.slug) || a.slug.localeCompare(b.slug));
}

function titleOf(slug) {
  return slug
    .split('-')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

/**
 * Every file of the frame: published path (relative to `/_explorer/`) to its bytes.
 *
 * @param {string} [webRoot]
 * @returns {Map<string, Buffer | string>}
 */
export function explorerFiles(webRoot = WEB_ROOT) {
  const scalar = scalarPackage(webRoot);
  const pinned = pinnedVersion(webRoot);
  if (scalar.version !== pinned) {
    throw new Error(
      `${SCALAR_PACKAGE} ${scalar.version} installed, ${pinned} pinned: run pnpm install`,
    );
  }
  const files = new Map();
  files.set('scalar.js', readFileSync(join(scalar.dir, 'dist', 'browser', 'standalone.js')));
  for (const name of GLUE) files.set(name, readFileSync(join(webRoot, 'api-explorer', name)));

  const page = readFileSync(join(webRoot, 'api-explorer', 'index.html'), 'utf8')
    .replace('%SCALAR_INTEGRITY%', integrity(files.get('scalar.js')))
    .replace('%EXPLORER_JS_INTEGRITY%', integrity(files.get('explorer.js')))
    .replace('%EXPLORER_CSS_INTEGRITY%', integrity(files.get('explorer.css')));
  const left = page.match(/%[A-Z_]+%/);
  if (left) throw new Error(`api-explorer/index.html: placeholder ${left[0]} not filled`);
  files.set('index.html', page);

  const documents = explorerDocuments(join(webRoot, 'openapi'));
  files.set(
    'openapi/index.json',
    JSON.stringify(documents.map(({ slug, title }) => ({ slug, title }))),
  );
  for (const { slug, document } of documents) {
    files.set(`openapi/${slug}.json`, JSON.stringify(document));
  }
  files.set(
    'LICENSE.txt',
    [
      `${SCALAR_PACKAGE} ${scalar.version} (${scalar.license}), https://github.com/scalar/scalar`,
      'The standalone bundle (scalar.js) contains Scalar and its dependencies; their notices are',
      'in the bundle and in the package on npm.',
      '',
      'MIT License',
      '',
      'Copyright (c) 2023-present Scalar',
      '',
      'Permission is hereby granted, free of charge, to any person obtaining a copy of this software',
      'and associated documentation files (the "Software"), to deal in the Software without',
      'restriction, including without limitation the rights to use, copy, modify, merge, publish,',
      'distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the',
      'Software is furnished to do so, subject to the following conditions:',
      '',
      'The above copyright notice and this permission notice shall be included in all copies or',
      'substantial portions of the Software.',
      '',
      'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING',
      'BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND',
      'NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,',
      'DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,',
      'OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.',
      '',
    ].join('\n'),
  );
  return files;
}

/** Write the frame into `<outDir>/_explorer/` (replacing an earlier one). */
export function writeExplorer(outDir, webRoot = WEB_ROOT) {
  const target = join(outDir, EXPLORER_BASE.replace(/\//g, ''));
  rmSync(target, { recursive: true, force: true });
  const files = explorerFiles(webRoot);
  for (const [path, bytes] of files) {
    mkdirSync(dirname(join(target, path)), { recursive: true });
    writeFileSync(join(target, path), bytes);
  }
  return { target, count: files.size };
}

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.txt': 'text/plain; charset=utf-8',
};

/**
 * Vite plugin for `pnpm dev`: serves the frame under `/_explorer/` from the sources (the build
 * writes it with `writeExplorer` instead). The dev server sends no CSP; the production policy is
 * nginx's.
 *
 * @returns {import('vite').Plugin}
 */
export function apiExplorerDev(webRoot = WEB_ROOT) {
  return {
    name: 'mowdeck:api-explorer-dev',
    apply: 'serve',
    configureServer(server) {
      server.middlewares.use(EXPLORER_BASE, (request, response, next) => {
        const path = (request.url ?? '/').split('?')[0].replace(/^\//, '') || 'index.html';
        let file;
        try {
          file = explorerFiles(webRoot).get(path);
        } catch (error) {
          next(error);
          return;
        }
        if (file === undefined) {
          response.statusCode = 404;
          response.end();
          return;
        }
        response.setHeader(
          'Content-Type',
          TYPES[path.slice(path.lastIndexOf('.'))] ?? 'application/octet-stream',
        );
        response.setHeader('Cache-Control', 'no-cache');
        response.end(file);
      });
    },
  };
}

function readJson(path) {
  return JSON.parse(readFileSync(path, 'utf8'));
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [command, out = 'build'] = process.argv.slice(2);
  if (command !== 'build') {
    console.error('usage: node build-tools/api-explorer.mjs build [outDir]');
    process.exit(2);
  }
  const { target, count } = writeExplorer(resolve(out));
  console.log(
    `API explorer: ${count} file(s) in ${target} (served only with MOWDECK_WEB_API_EXPLORER=true)`,
  );
}

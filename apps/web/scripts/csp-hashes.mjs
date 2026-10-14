#!/usr/bin/env node
// Computes the CSP script hashes of the built app and writes the nginx snippet
// (build/csp.conf, copied to /etc/nginx/snippets/csp.conf by the Dockerfile).
//
// SvelteKit's SPA fallback (200.html) boots with one inline <script>; the CSP allows exactly the
// hashes of the inline scripts found in the built HTML and nothing else inline. Styles are files
// only ('self'); runtime style changes go through the CSSOM, which the CSP does not restrict.
// Static style="" attributes inside compiled templates (SvelteKit's route announcer) are allowed
// by hash with 'unsafe-hashes'. Fails when an inline <style> or a style="" attribute appears in
// built HTML.
//
// The policy names no third party. nginx/40-mowdeck-runtime-config.sh adds the tile server to
// img-src and, with MOWDECK_WEB_CAMERA_ENABLED=true, the Agora domains to connect-src and blob:
// to media-src and worker-src (ADR 0032), matching the directives below literally.
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const dir = process.argv[2] ?? 'build';

function filesWith(root, extension) {
  return readdirSync(root).flatMap((name) => {
    const path = join(root, name);
    if (statSync(path).isDirectory()) return filesWith(path, extension);
    return name.endsWith(extension) ? [path] : [];
  });
}

const sha256 = (text) => `'sha256-${createHash('sha256').update(text).digest('base64')}'`;

const hashes = new Set();
const problems = [];
for (const file of filesWith(dir, '.html')) {
  const html = readFileSync(file, 'utf8');
  for (const match of html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)) {
    const body = match[1];
    if (body.trim()) hashes.add(`'sha256-${createHash('sha256').update(body).digest('base64')}'`);
  }
  if (/<style[\s>]/.test(html)) problems.push(`${file}: inline <style>`);
  if (/\sstyle="/.test(html)) problems.push(`${file}: style="" attribute`);
}

const styleHashes = new Set();
for (const file of filesWith(dir, '.js')) {
  for (const match of readFileSync(file, 'utf8').matchAll(/style="([^"]*)"/g)) {
    styleHashes.add(sha256(match[1]));
  }
}

if (problems.length) {
  console.error(`CSP: inline styles are not allowed:\n${problems.join('\n')}`);
  process.exit(1);
}

const policy = [
  "default-src 'self'",
  `script-src 'self' ${[...hashes].sort().join(' ')}`.trim(),
  styleHashes.size
    ? `style-src 'self' 'unsafe-hashes' ${[...styleHashes].sort().join(' ')}`
    : "style-src 'self'",
  "img-src 'self' data: blob:",
  "font-src 'self'",
  "media-src 'self'",
  "connect-src 'self'",
  "worker-src 'self'",
  "manifest-src 'self'",
  "frame-ancestors 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "object-src 'none'",
].join('; ');

writeFileSync(join(dir, 'csp.conf'), `add_header Content-Security-Policy "${policy}" always;\n`);
console.log(
  `CSP: ${hashes.size} script and ${styleHashes.size} style attribute hash(es) in ${join(dir, 'csp.conf')}`,
);

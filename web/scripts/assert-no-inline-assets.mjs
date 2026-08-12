/**
 * Fails when the built bundle contains an inline script or style.
 *
 * The API serves a Content-Security-Policy with `script-src 'self'` and no
 * `unsafe-inline`. That header is only serveable because `assetsInlineLimit: 0`
 * in vite.config.ts keeps every script and style in its own file with its own
 * URL. Raise that limit -- or take a bundler default that reintroduces
 * inlining -- and the page silently stops working under its own CSP, or worse,
 * somebody widens the CSP to make it work again.
 *
 * The CI comment on the build step claimed for two months that building the
 * bundle "proves assetsInlineLimit: 0 still holds". It did not: `vite build`
 * succeeds either way. This is the check that comment was describing.
 *
 * Usage: node scripts/assert-no-inline-assets.mjs [dist-dir]
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import process from 'node:process';

const dist = process.argv[2] ?? 'dist';

/** A tag with anything but whitespace between its open and close. */
const INLINE_SCRIPT = /<script(?![^>]*\bsrc=)[^>]*>\s*\S[\s\S]*?<\/script>/i;
const INLINE_STYLE = /<style[^>]*>\s*\S[\s\S]*?<\/style>/i;

/** A stylesheet pulled in as a data: URI rather than a file. */
const DATA_URI_HREF = /<link[^>]+href=["']data:/i;

function htmlFiles(dir) {
  const found = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) found.push(...htmlFiles(path));
    else if (entry.endsWith('.html')) found.push(path);
  }
  return found;
}

let pages = [];
try {
  pages = htmlFiles(dist);
} catch (error) {
  console.error(`cannot read ${dist}: ${error.message}`);
  console.error('run `npm run build` first');
  process.exit(2);
}

if (pages.length === 0) {
  // An empty dist passes every check below, which would make this script a
  // control that reports success when it has examined nothing.
  console.error(`no HTML found under ${dist}/ -- nothing was checked`);
  process.exit(2);
}

const problems = [];
for (const page of pages) {
  const html = readFileSync(page, 'utf8');
  if (INLINE_SCRIPT.test(html)) problems.push(`${page}: inline <script> body`);
  if (INLINE_STYLE.test(html)) problems.push(`${page}: inline <style> block`);
  if (DATA_URI_HREF.test(html)) problems.push(`${page}: stylesheet as a data: URI`);
}

if (problems.length > 0) {
  console.error('The bundle inlines assets the served CSP forbids:\n');
  for (const problem of problems) console.error(`  - ${problem}`);
  console.error(
    '\nThe API sends `script-src \'self\'` with no `unsafe-inline`' +
      ' (src/api/middleware.py). Either restore `assetsInlineLimit: 0` in' +
      ' vite.config.ts, or change the CSP deliberately and say so in' +
      ' docs/operations/SECURITY.md -- do not widen it to make a build pass.',
  );
  process.exit(1);
}

console.log(`no inline scripts or styles in ${pages.length} page(s) under ${dist}/`);

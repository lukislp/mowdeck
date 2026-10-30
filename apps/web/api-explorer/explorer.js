/**
 * The API explorer's frame (ADR 0072), page part: waits for the access token from the web app,
 * loads the services' OpenAPI documents the build ships next to it and mounts Scalar (the pinned,
 * self-hosted standalone bundle `scalar.js`, loaded before this module). A renewed token updates
 * the running instance; a logout (null) empties it.
 */
import {
  READY_MESSAGE,
  parseIndex,
  prepareDocument,
  scalarConfiguration,
  tokenFromMessage,
} from './frame.js';

const origin = window.location.origin;
const root = /** @type {HTMLElement} */ (document.getElementById('explorer'));
const dark =
  new URLSearchParams(window.location.search).get('theme') === 'dark' ||
  (new URLSearchParams(window.location.search).get('theme') !== 'light' &&
    window.matchMedia('(prefers-color-scheme: dark)').matches);

/** @type {string | null} */
let token = null;
/** @type {{ updateConfiguration(config: object): void } | null} */
let instance = null;
/** @type {{ slug: string; title: string; content: Record<string, any> }[]} */
let sources = [];

async function loadSources() {
  const index = parseIndex(await (await fetch('openapi/index.json')).json());
  return Promise.all(
    index.map(async ({ slug, title }) => {
      const response = await fetch(`openapi/${slug}.json`);
      return { slug, title, content: prepareDocument(await response.json(), origin) };
    }),
  );
}

function render() {
  const config = scalarConfiguration({ sources, token, dark }, origin);
  if (instance) {
    instance.updateConfiguration(config);
    return;
  }
  root.textContent = '';
  // @ts-expect-error -- the standalone bundle defines window.Scalar
  instance = window.Scalar.createApiReference(root, config);
}

window.addEventListener('message', (event) => {
  const next = tokenFromMessage(event, { origin, parent: window.parent });
  if (next === undefined) return;
  token = next;
  if (sources.length) render();
});

loadSources()
  .then((loaded) => {
    sources = loaded;
    render();
  })
  .catch(() => {
    root.textContent = 'The API documents could not be loaded.';
  });

if (window.parent !== window) window.parent.postMessage({ type: READY_MESSAGE }, origin);

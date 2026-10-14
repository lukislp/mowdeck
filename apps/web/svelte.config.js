import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** SvelteKit 2 as a static SPA: every route is client rendered, unknown paths fall back to
 * 200.html (served by nginx with try_files). */
const config = {
  preprocess: vitePreprocess(),
  compilerOptions: { runes: true },
  kit: {
    adapter: adapter({ pages: 'build', assets: 'build', fallback: '200.html', strict: true }),
    serviceWorker: { register: false },
    version: { pollInterval: 0 },
  },
};

export default config;

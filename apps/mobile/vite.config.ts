// Builds the bundled pages of the shells into www/ (Capacitor's webDir): the first-launch
// server screen (index.html) and the offline page (offline.html). The web app itself is not
// bundled; it is loaded from the MowDeck server.
import { resolve } from 'node:path';

import { defineConfig } from 'vitest/config';

export default defineConfig({
  base: './',
  build: {
    outDir: 'www',
    emptyOutDir: true,
    target: 'es2022',
    rollupOptions: {
      input: {
        index: resolve(import.meta.dirname, 'index.html'),
        offline: resolve(import.meta.dirname, 'offline.html'),
      },
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});

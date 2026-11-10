// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

// Builds the bundled pages of the shells into www/ (Capacitor's webDir): the first-launch
// server screen (index.html), the offline page (offline.html) and the About page (about.html),
// plus public/ (the generated third-party notices) as it is. The web app itself is not bundled;
// it is loaded from the MowDeck server.
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
        about: resolve(import.meta.dirname, 'about.html'),
      },
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});

import { paraglideVitePlugin } from '@inlang/paraglide-js';
import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { svelteTesting } from '@testing-library/svelte/vite';
import { defineConfig } from 'vitest/config';

import { apiExplorerDev } from './build-tools/api-explorer.mjs';
import { agoraIncluded, releaseLicences } from './build-tools/release-licences.js';

/** The Compose stack's gateway; `pnpm dev` proxies the API (and the WebSocket) to it so the
 * app and the API share one origin, exactly like the `web` service behind Traefik. */
const gateway = process.env.MOWDECK_DEV_GATEWAY ?? 'http://localhost';

/** The Agora Web SDK only in an operator's own build (MOWDECK_WEB_AGORA_SDK=include); every
 * published image is built without it (build-tools/release-licences.ts, apps/web/README.md). */
const agora = agoraIncluded(process.env.MOWDECK_WEB_AGORA_SDK);

export default defineConfig({
  plugins: [
    // SvelteKit 3 as a static SPA (its options live here, there is no svelte.config.js): every
    // route is client rendered, unknown paths fall back to 200.html (served by nginx with
    // try_files). The app registers the service worker itself (update prompt, native shell).
    sveltekit({
      preprocess: vitePreprocess(),
      compilerOptions: { runes: true },
      adapter: adapter({
        pages: 'build',
        assets: 'build',
        fallback: '200.html',
        strict: true,
      }),
      serviceWorker: { register: false },
      version: { pollInterval: 0 },
    }),
    paraglideVitePlugin({
      project: './project.inlang',
      outdir: './src/lib/paraglide',
      strategy: ['localStorage', 'preferredLanguage', 'baseLocale'],
    }),
    svelteTesting(),
    ...releaseLicences(agora),
    // The API explorer's frame (ADR 0072) under /_explorer/ in `pnpm dev`; the build writes it
    // after the SPA (build-tools/api-explorer.mjs).
    apiExplorerDev(),
  ],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: gateway,
        changeOrigin: true,
        ws: true,
        // Realtime checks the Origin of sockets; present the gateway's origin.
        configure: (proxy) => {
          proxy.on('proxyReqWs', (request) => request.setHeader('origin', gateway));
        },
      },
    },
  },
  build: {
    target: 'es2022',
    // The CSP has no 'unsafe-inline' for styles: keep CSS in files, never inline it.
    assetsInlineLimit: 0,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.ts', 'build-tools/**/*.test.ts', 'api-explorer/**/*.test.ts'],
    setupFiles: ['src/tests/setup.ts'],
    coverage: {
      provider: 'v8',
      include: [
        'src/lib/auth/session.svelte.ts',
        'src/lib/realtime/client.svelte.ts',
        'src/lib/stores/mowers.svelte.ts',
        'src/lib/stores/commands.svelte.ts',
        'src/lib/api/problem.ts',
        'src/lib/mower/commands.ts',
        'src/lib/mower/route.ts',
        'src/lib/mower/job.ts',
        'src/lib/mower/reception.ts',
        'src/lib/config.ts',
        'src/lib/push/device.ts',
        'src/lib/push/payload.ts',
        'src/lib/stores/inbox.svelte.ts',
        'src/lib/connection/form.ts',
        'src/lib/linked/accounts.ts',
        'src/lib/schedules/form.ts',
        'src/lib/schedules/preview.ts',
        'src/lib/schedules/recurrence.ts',
        'src/lib/mower/rtk-range.ts',
        'src/lib/mower/mow-pattern.ts',
        'src/lib/mower/device-settings.ts',
        'src/lib/mower/device-settings.svelte.ts',
        'src/lib/drive/pilot.ts',
        'src/lib/drive/layout.ts',
        'src/lib/mower/maintenance.ts',
        'src/lib/health/series.ts',
        'src/lib/plan/usage.ts',
        'src/lib/settings/token-permissions.ts',
        'src/lib/plan/problems.ts',
        'src/lib/operator/plans.ts',
        'src/lib/operator/assign.ts',
        'src/lib/operator/system.ts',
        'src/lib/billing/billing.ts',
        'src/lib/billing/account.svelte.ts',
        'src/lib/legal/markdown.ts',
        'src/lib/legal/source.ts',
        'src/lib/camera/session.ts',
        'src/lib/camera/test-pattern.ts',
        'src/lib/camera/player.ts',
        'src/lib/camera/factory.ts',
        'src/lib/camera/viewer.svelte.ts',
        'src/lib/camera/freeze.ts',
        'src/lib/vision/api.ts',
        'src/lib/vision/assist.svelte.ts',
        'src/lib/vision/labels.ts',
        'src/lib/vision/problems.ts',
        'src/lib/vision/provider.ts',
        'src/lib/vision/snapshot.ts',
        'src/lib/native/capacitor.ts',
        'src/lib/native/links.ts',
        'src/lib/native/local.ts',
        'src/lib/native/apns.ts',
        'src/lib/native/shell.svelte.ts',
        'src/lib/navigate.ts',
      ],
      reporter: ['text-summary'],
      thresholds: { lines: 80 },
    },
  },
});

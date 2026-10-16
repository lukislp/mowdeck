import { paraglideVitePlugin } from '@inlang/paraglide-js';
import { sveltekit } from '@sveltejs/kit/vite';
import { svelteTesting } from '@testing-library/svelte/vite';
import { defineConfig } from 'vitest/config';

/** The Compose stack's gateway; `pnpm dev` proxies the API (and the WebSocket) to it so the
 * app and the API share one origin, exactly like the `web` service behind Traefik. */
const gateway = process.env.MOWDECK_DEV_GATEWAY ?? 'http://localhost';

export default defineConfig({
  plugins: [
    sveltekit(),
    paraglideVitePlugin({
      project: './project.inlang',
      outdir: './src/lib/paraglide',
      strategy: ['localStorage', 'preferredLanguage', 'baseLocale'],
    }),
    svelteTesting(),
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
    include: ['src/**/*.test.ts'],
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
        'src/lib/drive/pilot.ts',
        'src/lib/drive/layout.ts',
        'src/lib/health/series.ts',
        'src/lib/plan/usage.ts',
        'src/lib/plan/problems.ts',
        'src/lib/operator/plans.ts',
        'src/lib/operator/assign.ts',
        'src/lib/operator/system.ts',
        'src/lib/billing/billing.ts',
        'src/lib/billing/account.svelte.ts',
        'src/lib/legal/markdown.ts',
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
      ],
      reporter: ['text-summary'],
      thresholds: { lines: 80 },
    },
  },
});

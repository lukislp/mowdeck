<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  Report a problem: one button that opens the repository's bug report form on GitHub in a new
  tab, the environment fields filled in from the running app (`report-problem.ts`). Environment
  data only (version, platform, browser, locale, demo or not, model and link of the first
  mower); no tokens, account names, serial numbers, positions, mower ids or logs. It is a plain
  external link, so it works on the demo too (the demo only locks forms the services refuse).

  The first mower is looked up when the section appears, so the click itself is synchronous
  (a popup blocker only lets a new tab open straight from the click). Without a mower, or when
  the lookup fails, the field stays empty.
-->
<script lang="ts">
  import { Button, Panel } from '@mowdeck/ui';

  import { fleetApi, telemetryApi, unwrap } from '#lib/api/client.js';
  import { loadConfig } from '#lib/config.js';
  import { demo } from '#lib/demo/state.svelte.js';
  import { DEFAULT_SOURCE, type SourceInfo } from '#lib/legal/source.js';
  import { nativeShell } from '#lib/native/runtime.js';
  import { getLocale } from '#lib/paraglide/runtime.js';
  import { m } from '#lib/paraglide/messages.js';

  import { firstMower, gatherEnvironment, type EnvironmentDeps } from './environment';
  import { reportProblemUrl } from './report-problem';

  interface Props {
    /** The build; read from `/config.json` when not given (tests). */
    source?: SourceInfo;
    /** Opens the address; the default opens a new tab without opener and referrer. */
    open?: (url: string) => void;
  }
  let { source, open = openInNewTab }: Props = $props();

  let loaded = $state<SourceInfo>(DEFAULT_SOURCE);
  let mower = $state<EnvironmentDeps['mower']>(null);

  $effect(() => {
    if (source) return;
    void loadConfig().then((config) => (loaded = config.source));
  });

  $effect(() => {
    // The model as the mower list shows it (the capabilities' own name, else Fleet's); a
    // failing capabilities request only leaves Fleet's value.
    const capabilities = unwrap(
      telemetryApi.GET('/api/v1/telemetry/accounts/me/mowers/capabilities'),
    )
      .then((found) => found.items)
      .catch(() => []);
    void Promise.all([unwrap(fleetApi.GET('/api/v1/fleet/mowers')), capabilities])
      .then(([list, caps]) => (mower = firstMower(list.items, caps)))
      .catch(() => (mower = null));
  });

  function openInNewTab(url: string): void {
    window.open(url, '_blank', 'noopener,noreferrer');
  }

  function report(): void {
    open(
      reportProblemUrl(
        gatherEnvironment({
          source: source ?? loaded,
          userAgent: navigator.userAgent,
          shellPlatform: nativeShell.platform,
          locale: getLocale(),
          demo: demo.active,
          mower,
        }),
      ),
    );
  }
</script>

<Panel label={m.report_problem_label()} testid="report-problem-panel">
  <div class="stack">
    <p class="text">{m.report_problem_text()}</p>
    <div class="action">
      <Button icon="send" testid="report-problem" onclick={report}
        >{m.report_problem_button()}<span class="sr"> ({m.source_new_tab()})</span></Button
      >
    </div>
  </div>
</Panel>

<style>
  .stack {
    display: grid;
    gap: var(--md-space-3);
    padding: 0 var(--md-gutter) var(--md-space-2);
  }

  .text {
    max-width: 60ch;
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .action {
    justify-self: start;
  }

  .sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
</style>

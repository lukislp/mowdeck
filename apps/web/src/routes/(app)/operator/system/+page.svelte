<!--
  Operator console, system: the installation facts the app can read (billing switch, the plan
  marked default, offered plans, accounts, the feature catalogue, the modules that announce web
  areas) and whether each service answers a public `GET` through the gateway, with status and
  latency measured in this browser. The services' `/readyz` is not routed, so this is
  reachability, not readiness; there are no other metrics here. Response bodies are dropped.
-->
<script lang="ts">
  import { Button, Icon, TopBar } from '@mowdeck/ui';

  import { apiFetch, entitlementsApi, identityApi, unwrap } from '$lib/api/client';
  import ToneBadge from '$lib/components/ToneBadge.svelte';
  import { formatNumber, formatTime } from '$lib/i18n';
  import { realtime } from '$lib/live.svelte';
  import { verdictLabel } from '$lib/operator/labels';
  import {
    probeService,
    probeVisionDefault,
    SERVICE_PROBES,
    type ProbeResult,
    type VisionDefault,
  } from '$lib/operator/system';
  import { m } from '$lib/paraglide/messages';
  import { accountPlan } from '$lib/plan/plan.svelte';
  import type { Feature, Plan } from '$lib/plan/types';
  import type { Glyph, Tone } from '$lib/schedules/labels';

  interface Area {
    module: string;
    module_version?: string | null;
    route: string;
  }

  let results = $state<ProbeResult[]>([]);
  let probing = $state(false);
  let probedAt = $state<number | null>(null);
  let plans = $state<Plan[] | null>(null);
  let features = $state<Feature[] | null>(null);
  let accounts = $state<number | null>(null);
  let areas = $state<Area[] | null>(null);
  /** The hosted vision default (undefined: loading; null: Vision does not answer). */
  let vision = $state<VisionDefault | null | undefined>(undefined);

  async function probeAll(): Promise<void> {
    probing = true;
    results = await Promise.all(SERVICE_PROBES.map((probe) => probeService(probe, apiFetch)));
    probedAt = Date.now();
    probing = false;
  }

  $effect(() => {
    void probeAll();
    void probeVisionDefault(apiFetch).then((found) => (vision = found));
    void accountPlan.load(true);
    void unwrap(entitlementsApi.GET('/api/v1/entitlements/plans'))
      .then((list) => (plans = list))
      .catch(() => (plans = null));
    void unwrap(entitlementsApi.GET('/api/v1/entitlements/features'))
      .then((list) => (features = list))
      .catch(() => (features = null));
    void unwrap(
      identityApi.GET('/api/v1/identity/platform/accounts', { params: { query: { limit: 1 } } }),
    )
      .then((page) => (accounts = page.total))
      .catch(() => (accounts = null));
    void unwrap(identityApi.GET('/api/v1/identity/ui-areas'))
      .then((list) => (areas = list as Area[]))
      .catch(() => (areas = null));
  });

  const billing = $derived(accountPlan.current?.billing_enabled);
  const defaultPlan = $derived(plans?.find((p) => p.is_default) ?? null);
  const modules = $derived.by(() => {
    const found = new Map<string, string>();
    for (const area of areas ?? []) found.set(area.module, area.module_version ?? '');
    for (const feature of features ?? [])
      if (!found.has(feature.module)) found.set(feature.module, '');
    return [...found.entries()].sort(([a], [b]) => a.localeCompare(b));
  });

  function look(result: ProbeResult): { tone: Tone; glyph: Glyph } {
    switch (result.verdict) {
      case 'up':
        return { tone: 'mowing', glyph: 'check' };
      case 'degraded':
        return { tone: 'paused', glyph: 'alert' };
      case 'down':
        return { tone: 'error', glyph: 'alert' };
    }
  }
</script>

<svelte:head><title>{m.op_system_title()} · {m.app_name()}</title></svelte:head>

<TopBar
  title={m.op_system_title()}
  meta={probedAt ? m.op_probed_at({ time: formatTime(probedAt) }) : undefined}
>
  {#snippet actions()}
    <Button
      icon="signal"
      label={m.op_probe_again()}
      pending={probing}
      testid="system-probe"
      onclick={probeAll}
    />
  {/snippet}
</TopBar>

<div class="layout">
  <section class="card" aria-labelledby="facts-head" data-testid="system-facts">
    <h2 id="facts-head">{m.op_installation()}</h2>
    <dl class="facts">
      <div class="fact" data-testid="system-billing" data-value={String(billing ?? '')}>
        <dt>{m.op_fact_billing()}</dt>
        <dd>
          {#if billing === undefined}
            <span class="quiet">{m.no_data()}</span>
          {:else}
            <span class="switch" data-on={billing}>{billing ? m.switch_on() : m.switch_off()}</span>
          {/if}
        </dd>
        <span class="note"
          >{billing === false ? m.op_fact_billing_off() : m.op_fact_billing_on()}</span
        >
      </div>
      <div
        class="fact"
        data-testid="system-vision"
        data-value={vision === undefined ? '' : String(vision?.enabled ?? 'absent')}
      >
        <dt>{m.op_fact_vision()}</dt>
        <dd>
          {#if vision === undefined}
            <span class="quiet">{m.no_data()}</span>
          {:else if vision === null}
            <span class="quiet small">{m.op_fact_vision_absent()}</span>
          {:else}
            <span class="switch" data-on={vision.enabled}
              >{vision.enabled ? m.switch_on() : m.switch_off()}</span
            >
          {/if}
        </dd>
        <span class="note">
          {#if vision?.enabled && vision.model}
            <span class="mono">{vision.model}</span>
          {:else if vision}
            {m.op_fact_vision_off()}
          {:else}
            {m.op_fact_vision_note()}
          {/if}
        </span>
      </div>
      <div class="fact">
        <dt>{m.op_fact_default_plan()}</dt>
        <dd class="mono">{defaultPlan?.key ?? m.no_data()}</dd>
        <span class="note">{m.op_fact_default_plan_note()}</span>
      </div>
      <div class="fact">
        <dt>{m.op_fact_plans()}</dt>
        <dd class="mono">{plans ? formatNumber(plans.length) : m.no_data()}</dd>
      </div>
      <div class="fact">
        <dt>{m.op_fact_accounts()}</dt>
        <dd class="mono">{accounts === null ? m.no_data() : formatNumber(accounts)}</dd>
      </div>
      <div class="fact">
        <dt>{m.op_fact_features()}</dt>
        <dd class="mono">{features ? formatNumber(features.length) : m.no_data()}</dd>
      </div>
    </dl>
    {#if modules.length}
      <h3>{m.op_fact_modules()}</h3>
      <ul class="modules" data-testid="system-modules">
        {#each modules as [module, version] (module)}
          <li>
            <span class="mono">{module}</span>{#if version}<span class="ver">{version}</span>{/if}
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  <section class="card services" aria-labelledby="services-head">
    <h2 id="services-head">{m.op_services()}</h2>
    <p class="quiet small">{m.op_services_hint()}</p>
    <ul class="probes" data-testid="system-services">
      {#each results as result (result.service)}
        <li
          class="probe"
          data-testid="system-service-{result.service}"
          data-verdict={result.verdict}
        >
          <span class="svc">
            <span class="svc-name">{result.service}</span>
            <span class="path">{result.path}</span>
          </span>
          <span class="code mono">{result.status ?? m.no_data()}</span>
          <span class="latency mono"
            >{result.latencyMs === null
              ? m.no_data()
              : m.op_latency({ ms: formatNumber(result.latencyMs) })}</span
          >
          <ToneBadge
            {...look(result)}
            label={verdictLabel(result.verdict)}
            value={result.verdict}
          />
        </li>
      {:else}
        <li class="quiet" role="status">{m.loading()}</li>
      {/each}
      <li class="probe" data-testid="system-service-realtime">
        <span class="svc">
          <span class="svc-name">{m.op_realtime()}</span>
          <span class="path">{m.op_realtime_note()}</span>
        </span>
        <span class="code mono"><Icon name="signal" size={16} /></span>
        <span class="latency"
          >{realtime.phase === 'ready' ? m.live_ready() : m.op_realtime_idle()}</span
        >
        {#if realtime.phase === 'ready'}
          <ToneBadge tone="mowing" glyph="check" label={m.op_probe_up()} value="up" />
        {:else}
          <ToneBadge tone="offline" glyph="dash" label={m.op_probe_none()} value="none" />
        {/if}
      </li>
    </ul>
  </section>
</div>

<style>
  .layout {
    display: grid;
    gap: var(--md-space-4);
    padding: var(--md-space-4) var(--md-gutter) var(--md-space-8);
  }

  .card {
    display: grid;
    align-content: start;
    gap: var(--md-space-3);
    padding: var(--md-space-4);
    border: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  h2,
  h3,
  dt {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .quiet {
    margin: 0;
    color: var(--md-color-ink-muted);
  }

  .small {
    font-size: var(--md-font-size-13);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .facts {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    margin: 0;
    border: var(--md-hairline) solid var(--md-color-line);
  }

  .fact {
    display: grid;
    align-content: start;
    gap: var(--md-space-1);
    padding: var(--md-space-3);
    border-inline-end: var(--md-hairline) solid var(--md-color-line);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  dd {
    margin: 0;
    font-size: var(--md-font-size-24);
    font-weight: var(--md-weight-medium);
  }

  .note {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  .switch {
    display: inline-block;
    padding: 0 var(--md-space-2);
    border: var(--md-border-control) solid var(--md-color-ink);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-16);
    line-height: var(--md-line-28);
  }

  .switch[data-on='true'] {
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .modules {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .modules li {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    padding: 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-28);
  }

  .ver {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-11);
  }

  .probes {
    display: grid;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .probe {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas:
      'svc badge'
      'code latency';
    align-items: center;
    gap: var(--md-space-1) var(--md-space-3);
    min-height: var(--md-target);
    padding: var(--md-space-2) 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .svc {
    grid-area: svc;
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .svc-name {
    font-weight: var(--md-weight-semibold);
  }

  .path {
    overflow: hidden;
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .code {
    grid-area: code;
    font-size: var(--md-font-size-14);
  }

  .latency {
    grid-area: latency;
    justify-self: end;
    font-size: var(--md-font-size-14);
  }

  .probe > :global(.badge) {
    grid-area: badge;
    justify-self: end;
  }

  @media (min-width: 1024px) {
    .layout {
      grid-template-columns: minmax(0, 2fr) minmax(0, 3fr);
    }

    .probe {
      grid-template-columns: minmax(0, 1fr) 48px 168px 136px;
      grid-template-areas: 'svc code latency badge';
    }
  }
</style>

<!--
  The live camera of a mower on its own page (ADR 0032): the camera panel as large as the screen
  allows, the mower's state line and the emergency stop (`StopBar`), which stays in view on every
  mower sub-page. Leaving the page leaves the video channel.
-->
<script lang="ts">
  import { ProblemNotice, StatusBadge, TopBar, type Activity } from '@mowdeck/ui';

  import { goto } from '$app/navigation';
  import { page } from '$app/state';

  import { fleetApi, telemetryApi, unwrap } from '$lib/api/client';
  import { asProblem, type ApiProblem } from '$lib/api/problem';
  import { clock } from '$lib/clock.svelte';
  import CameraPanel from '$lib/components/CameraPanel.svelte';
  import LiveStatus from '$lib/components/LiveStatus.svelte';
  import SlowLoading from '$lib/components/SlowLoading.svelte';
  import StopBar from '$lib/components/StopBar.svelte';
  import type { components as Fleet } from '$lib/generated/fleet';
  import { activityLabel, presenceLabel, problemMessage, valuesAgeLabel } from '$lib/i18n';
  import { realtime, revoked } from '$lib/live.svelte';
  import { m } from '$lib/paraglide/messages';
  import { mowers as states } from '$lib/stores/mowers.svelte';

  type Mower = Fleet['schemas']['MowerOut'];

  const id = $derived(page.params.id ?? '');

  let mower = $state<Mower | null>(null);
  let notFound = $state(false);
  let problem = $state<ApiProblem | null>(null);
  let barHeight = $state(120);

  async function load(mowerId: string): Promise<void> {
    mower = null;
    notFound = false;
    problem = null;
    const path = { params: { path: { mower_id: mowerId } } };
    try {
      mower = await unwrap(fleetApi.GET('/api/v1/fleet/mowers/{mower_id}', path));
    } catch (error) {
      const p = asProblem(error);
      if (p.status === 404) notFound = true;
      else problem = p;
      return;
    }
    void unwrap(telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/state', path))
      .then((snap) => states.upsert(mowerId, snap.state, 'rest'))
      .catch(() => undefined);
  }

  $effect(() => {
    void load(id);
  });
  $effect(() => realtime.watch([id]));
  $effect(() => clock.hold(5_000));

  const ms = $derived(states.get(id)?.state ?? null);
  const activity = $derived((ms?.activity ?? null) as Activity | null);
  const stale = $derived(states.isOffline(id, clock.now));
</script>

<svelte:head><title>{m.camera_title()} · {mower?.name ?? m.app_name()}</title></svelte:head>

{#if notFound || revoked.has(id)}
  <TopBar title={m.detail_not_found_title()} backHref="/mowers" backLabel={m.back()} />
  <div class="pad"><ProblemNotice tone="info" title={m.detail_not_found_title()} /></div>
{:else if problem}
  <TopBar title={m.camera_title()} backHref="/mowers/{id}" backLabel={m.back()} />
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if mower}
  <div class="camera-page" style:--bar-height="{barHeight}px" data-testid="camera-page">
    <TopBar title={m.camera_title()} meta={mower.name} backHref="/mowers/{id}" backLabel={m.back()}>
      {#snippet status()}
        <LiveStatus phase={realtime.phase} tooMany={realtime.tooMany} />
      {/snippet}
    </TopBar>
    <div class="state">
      {#if activity}
        <StatusBadge {activity} label={activityLabel(activity)} {stale} />
      {:else}
        <StatusBadge activity="unknown" label={m.activity_no_telemetry()} />
      {/if}
      {#if ms}
        {@const valuesAge = valuesAgeLabel(ms, clock.now)}
        <span class="age mono">{presenceLabel(ms, clock.now)}</span>
        {#if valuesAge}<span class="age mono values-age">{valuesAge}</span>{/if}
      {/if}
    </div>
    <div class="pad">
      {#key id}
        <CameraPanel
          mowerId={id}
          mowerName={mower.name}
          position={ms?.position ?? null}
          fullscreen
          onclose={() => void goto(`/mowers/${id}`)}
        />
      {/key}
    </div>
    <StopBar mowerId={id} bind:height={barHeight} />
  </div>
{:else}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{/if}

<style>
  .pad {
    padding: var(--md-space-3) var(--md-gutter);
  }

  .quiet {
    color: var(--md-color-ink-muted);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .camera-page {
    padding-bottom: calc(var(--bar-height) + var(--md-space-4));
  }

  .state {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    padding: var(--md-space-2) var(--md-gutter) 0;
  }

  .age {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .values-age {
    font-size: var(--md-font-size-12);
    opacity: 0.8;
  }
</style>

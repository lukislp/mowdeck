<!--
  The site of a mower: name, time zone and coordinates. The rain pause asks the weather at the
  site's coordinates (else the map origin, else the last position), and schedules fire in the
  site's time zone. The coordinates can be taken from the mower's map origin. Editing needs
  `can_manage_mowers` on the site; without it the values are shown read-only, and Fleet
  decides again on save (403 and 404 are shown in place).
-->
<script lang="ts">
  import { Button, Field, Panel, ProblemNotice, Readout } from '@mowdeck/ui';

  import { fleetApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import type { components } from '#lib/generated/fleet.js';
  import { formatNumber, problemMessage } from '#lib/i18n.js';
  import { permissions } from '#lib/live.svelte.js';
  import { m } from '#lib/paraglide/messages.js';
  import { isValidTimeZone, parseNumber } from '#lib/schedules/form.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  type Site = components['schemas']['SiteOut'];

  interface Props {
    siteId: string;
    /** The mower map's geo reference, offered as the coordinates. */
    origin?: { origin_lat: number; origin_lon: number } | null;
  }
  let { siteId, origin = null }: Props = $props();

  let site = $state<Site | null>(null);
  let canManage = $state(false);
  let loadProblem = $state<ApiProblem | null>(null);
  let saveProblem = $state<ApiProblem | null>(null);
  let busy = $state(false);
  let tried = $state(false);
  let name = $state('');
  let timezone = $state('');
  let latitude = $state('');
  let longitude = $state('');

  const deviceZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

  function show(found: Site): void {
    site = found;
    name = found.name;
    timezone = found.timezone;
    latitude = found.latitude != null ? String(found.latitude) : '';
    longitude = found.longitude != null ? String(found.longitude) : '';
  }

  $effect(() => {
    const current = siteId;
    void (async () => {
      loadProblem = null;
      try {
        show(
          await unwrap(
            fleetApi.GET('/api/v1/fleet/sites/{site_id}', {
              params: { path: { site_id: current } },
            }),
          ),
        );
      } catch (error) {
        loadProblem = asProblem(error);
        return;
      }
      try {
        const answers = await permissions.checkObjects([`site:${current}`], ['can_manage_mowers']);
        canManage = answers[`site:${current}`]?.can_manage_mowers ?? false;
      } catch {
        canManage = false;
      }
    })();
  });

  const issues = $derived.by(() => {
    const found: Record<'name' | 'timezone' | 'latitude' | 'longitude', string | null> = {
      name: null,
      timezone: null,
      latitude: null,
      longitude: null,
    };
    if (!tried) return found;
    if (!site?.is_default && !name.trim()) found.name = m.sched_err_required();
    if (!timezone.trim()) found.timezone = m.sched_err_required();
    else if (!isValidTimeZone(timezone.trim())) found.timezone = m.sched_err_timezone();
    const lat = latitude.trim() ? parseNumber(latitude) : null;
    const lon = longitude.trim() ? parseNumber(longitude) : null;
    if (latitude.trim() && (lat === null || lat < -90 || lat > 90)) {
      found.latitude = m.sched_err_range({ min: '-90', max: '90' });
    }
    if (longitude.trim() && (lon === null || lon < -180 || lon > 180)) {
      found.longitude = m.sched_err_range({ min: '-180', max: '180' });
    }
    if (!!latitude.trim() !== !!longitude.trim()) {
      found[latitude.trim() ? 'longitude' : 'latitude'] = m.site_err_both();
    }
    return found;
  });

  function useOrigin(): void {
    if (!origin) return;
    latitude = origin.origin_lat.toFixed(6);
    longitude = origin.origin_lon.toFixed(6);
  }

  async function save(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!site) return;
    tried = true;
    saveProblem = null;
    if (Object.values(issues).some(Boolean)) return;
    busy = true;
    try {
      show(
        await unwrap(
          fleetApi.PATCH('/api/v1/fleet/sites/{site_id}', {
            params: { path: { site_id: site.id } },
            body: {
              ...(site.is_default ? {} : { name: name.trim() }),
              timezone: timezone.trim(),
              latitude: latitude.trim() ? parseNumber(latitude) : null,
              longitude: longitude.trim() ? parseNumber(longitude) : null,
            },
          }),
        ),
      );
      tried = false;
      toasts.push('ok', m.saved());
    } catch (error) {
      saveProblem = asProblem(error);
    } finally {
      busy = false;
    }
  }

  const located = $derived(site?.latitude != null && site?.longitude != null);
</script>

<Panel label={m.site_title()} testid="site-location">
  {#if loadProblem}
    <ProblemNotice
      tone={loadProblem.status === 403 || loadProblem.status === 404 ? 'info' : 'error'}
      title={loadProblem.status === 403 || loadProblem.status === 404
        ? m.site_no_access()
        : problemMessage(loadProblem)}
      reference={loadProblem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  {:else if !site}
    <p class="hint" role="status"><SlowLoading /></p>
  {:else if !canManage}
    <p class="hint">{m.site_hint()}</p>
    <div class="readouts">
      <Readout label={m.site_name()} value={site.name} size="inline" emptyText={m.no_data()} />
      <Readout
        label={m.site_timezone()}
        value={site.timezone}
        size="inline"
        emptyText={m.no_data()}
      />
      <Readout
        label={m.site_coordinates()}
        value={located
          ? `${formatNumber(site.latitude ?? 0, 5)}, ${formatNumber(site.longitude ?? 0, 5)}`
          : null}
        size="inline"
        emptyText={m.site_no_coordinates()}
        testid="site-coordinates"
      />
    </div>
  {:else}
    <form class="form" onsubmit={save} novalidate>
      <p class="hint">{m.site_hint()}</p>
      <div class="fields">
        <Field
          id="site-name"
          label={m.site_name()}
          maxlength={100}
          disabled={site.is_default}
          hint={site.is_default ? m.site_default_hint() : undefined}
          bind:value={name}
          error={issues.name ?? saveProblem?.fieldError('name') ?? null}
        />
        <div class="tz">
          <Field
            id="site-timezone"
            label={m.site_timezone()}
            mono
            bind:value={timezone}
            error={issues.timezone ?? saveProblem?.fieldError('timezone') ?? null}
          />
          {#if timezone !== deviceZone}
            <button type="button" class="preset" onclick={() => (timezone = deviceZone)}
              >{m.sched_timezone_device({ zone: deviceZone })}</button
            >
          {/if}
        </div>
        <Field
          id="site-latitude"
          label={m.site_latitude()}
          inputmode="decimal"
          mono
          hint="°"
          bind:value={latitude}
          error={issues.latitude ?? saveProblem?.fieldError('latitude') ?? null}
        />
        <Field
          id="site-longitude"
          label={m.site_longitude()}
          inputmode="decimal"
          mono
          hint="°"
          bind:value={longitude}
          error={issues.longitude ?? saveProblem?.fieldError('longitude') ?? null}
        />
      </div>
      <div class="row">
        {#if origin}
          <Button icon="pin" testid="site-use-origin" onclick={useOrigin}
            >{m.site_use_origin()}</Button
          >
        {/if}
        {#if latitude || longitude}
          <Button
            variant="ghost"
            onclick={() => {
              latitude = '';
              longitude = '';
            }}>{m.site_clear_coordinates()}</Button
          >
        {/if}
      </div>
      {#if !located}
        <p class="note" data-testid="site-no-coordinates">{m.site_rain_fallback()}</p>
      {/if}
      {#if saveProblem}
        <ProblemNotice
          title={problemMessage(saveProblem)}
          detail={saveProblem.detail}
          reference={saveProblem.correlationId}
          referenceLabel={m.problem_reference()}
        />
      {/if}
      <div>
        <Button type="submit" variant="primary" pending={busy} testid="site-save">{m.save()}</Button
        >
      </div>
    </form>
  {/if}
</Panel>

<style>
  .form {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-3);
  }

  .hint {
    margin: 0 0 var(--md-space-3);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .form .hint {
    margin: 0;
  }

  .fields {
    display: grid;
    gap: var(--md-space-4);
  }

  .readouts {
    display: grid;
    gap: var(--md-space-3);
  }

  .tz {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--md-space-2);
  }

  .tz > :global(.md-field) {
    align-self: stretch;
  }

  .preset {
    min-height: 40px;
    padding: 0 var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    cursor: pointer;
  }

  .preset:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .note {
    margin: 0;
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 3px solid var(--md-state-offline);
    background: var(--md-color-surface-sunken);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  @media (min-width: 768px) {
    .fields {
      grid-template-columns: 1fr 1fr;
    }
  }
</style>

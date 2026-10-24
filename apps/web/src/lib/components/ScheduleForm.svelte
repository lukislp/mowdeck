<!--
  The schedule editor: target (a mower or a group), recurrence (weekday chips and a time, or
  every N days from an anchor date), the window, the areas of the target mower picked on its
  map in mowing order, the start settings with a rotation list, the rain pause and the time zone.
  The start settings use the start sheet's own controls (`MowOptions`), with exactly what the
  target offers: the mower's capabilities, for a group what every member offers (the members
  are listed with their models); a value left empty takes the mower's saved mow defaults, else
  its own setting. Changing the target clears what the new target does not offer.
  Client checks mirror the Scheduler's rules (`$lib/schedules/form`) and run on submit, then
  live; the server's 422 answer lands on the same fields. The page owns loading and saving.
-->
<script lang="ts">
  import {
    Button,
    Field,
    Icon,
    MowerMap,
    Panel,
    ProblemNotice,
    SegmentedControl,
    Select,
    Switch,
    type MapSnapshot,
  } from '@mowdeck/ui';
  import { tick, untrack, type Snippet } from 'svelte';

  import { telemetryApi } from '$lib/api/client';
  import type { ApiProblem } from '$lib/api/problem';
  import AreaPlan from '$lib/components/AreaPlan.svelte';
  import MowOptions from '$lib/components/MowOptions.svelte';
  import SlowLoading from '$lib/components/SlowLoading.svelte';
  import type { MapTilesConfig } from '$lib/config';
  import { formatNumber, problemMessage } from '$lib/i18n';
  import { displayModel } from '$lib/mower/limits';
  import { areaNamer, mapLabels } from '$lib/mower/map-labels';
  import { m } from '$lib/paraglide/messages';
  import { getLocale } from '$lib/paraglide/runtime';
  import {
    LIMITS,
    parseNumber,
    serverIssues,
    validateForm,
    type FormField,
    type FormIssue,
    type FormIssues,
    type ScheduleFormState,
  } from '$lib/schedules/form';
  import {
    NO_OFFER,
    commonOffer,
    fitMowDraft,
    mowBody,
    mowSummary,
    offersAnything,
    type CapsLike,
  } from '$lib/schedules/mow-options';
  import { formatDuration, weekdayShort } from '$lib/schedules/recurrence';
  import { WEEKDAYS, type Locale, type Weekday } from '$lib/schedules/types';

  interface Target {
    id: string;
    name: string;
    /** Mowers: the custom names of their map areas. */
    areaNames?: Readonly<Record<string, string>>;
    /** Groups: their mowers. */
    related?: readonly string[];
  }

  interface Props {
    form: ScheduleFormState;
    mowers: Target[];
    groups: Target[];
    /** Names of every mower the user sees, by id (a group's members). */
    mowerNames?: Readonly<Record<string, string>>;
    /** The mowers' capabilities by id; null while loading. */
    capabilities?: Readonly<Record<string, CapsLike>> | null;
    /** The effective time zone of the saved schedule (or the site's), for the hint. */
    siteTimeZone?: string | null;
    tiles?: MapTilesConfig | null;
    pending?: boolean;
    problem?: ApiProblem | null;
    submitLabel: string;
    onsubmit: (state: ScheduleFormState) => void;
    /** Extra content under the save button (delete). */
    footer?: Snippet;
  }
  let {
    form = $bindable(),
    mowers,
    groups,
    mowerNames = {},
    capabilities = null,
    siteTimeZone = null,
    tiles = null,
    pending = false,
    problem = null,
    submitLabel,
    onsubmit,
    footer,
  }: Props = $props();

  const switchStates = $derived({ on: m.switch_on(), off: m.switch_off() });
  const locale = $derived(getLocale() as Locale);
  const labels = $derived(mapLabels());

  let tried = $state(false);
  const server = $derived(serverIssues(problem));
  const issues = $derived<FormIssues>(tried ? { ...server, ...validateForm(form) } : server);

  // --- target ----------------------------------------------------------------------------------

  const targetKinds = $derived([
    { value: 'mower', label: m.sched_target_mower() },
    ...(groups.length || form.targetType === 'group'
      ? [{ value: 'group', label: m.sched_target_group() }]
      : []),
  ]);
  const targetOptions = $derived.by(() => {
    const list = (form.targetType === 'group' ? groups : mowers).map((t) => ({
      value: t.id,
      label: t.name,
    }));
    return form.targetId ? list : [{ value: '', label: m.sched_target_choose() }, ...list];
  });

  function setTargetType(value: string): void {
    if (value !== 'mower' && value !== 'group') return;
    form.targetType = value;
    const list = value === 'group' ? groups : mowers;
    form.targetId = list[0]?.id ?? '';
    form.areaIds = [];
  }

  // --- areas: the target mower's map snapshot --------------------------------------------------

  let mapSnapshot = $state.raw<MapSnapshot | null>(null);
  let mapState = $state<'idle' | 'loading' | 'none' | 'ready'>('idle');
  let mapFailed = $state(false);

  $effect(() => {
    const mowerId = form.targetType === 'mower' ? form.targetId : '';
    mapSnapshot = null;
    mapFailed = false;
    if (!mowerId) {
      mapState = 'idle';
      return;
    }
    let cancelled = false;
    mapState = 'loading';
    void telemetryApi
      .GET('/api/v1/telemetry/mowers/{mower_id}/map', {
        params: { path: { mower_id: mowerId } },
      })
      .then(({ data, response }) => {
        if (cancelled) return;
        if (response.ok && data) {
          mapSnapshot = data.map;
          mapState = 'ready';
          // Areas that are no longer on the map cannot be mowed: drop them.
          const known = new Set((data.map.areas ?? []).map((a) => a.id));
          const kept = untrack(() => form.areaIds).filter((id) => known.has(id));
          if (kept.length !== untrack(() => form.areaIds).length) form.areaIds = kept;
        } else {
          mapState = 'none';
        }
      })
      .catch(() => {
        if (!cancelled) mapState = 'none';
      });
    return () => {
      cancelled = true;
    };
  });

  const areas = $derived(mapSnapshot?.areas ?? []);
  // The same names as on the mower page: custom name, the mower's own, "Bereich N" in map order.
  const areaName = $derived(
    areaNamer(
      areas,
      form.targetType === 'mower' ? mowers.find((t) => t.id === form.targetId)?.areaNames : null,
    ),
  );

  function toggleArea(id: string): void {
    form.areaIds = form.areaIds.includes(id)
      ? form.areaIds.filter((a) => a !== id)
      : [...form.areaIds, id];
  }

  // --- recurrence ------------------------------------------------------------------------------

  const kindOptions = $derived([
    { value: 'weekly', label: m.sched_kind_weekly() },
    { value: 'every_n_days', label: m.sched_kind_every_n_days() },
  ]);

  function toggleDay(day: Weekday): void {
    form.days = form.days.includes(day)
      ? form.days.filter((d) => d !== day)
      : WEEKDAYS.filter((d) => d === day || form.days.includes(d));
  }

  function setDays(days: readonly Weekday[]): void {
    form.days = [...days];
  }

  const durationPreview = $derived.by(() => {
    const value = parseNumber(form.duration);
    return value && value > 0 ? formatDuration(value) : null;
  });

  // --- start settings: what the target's mowers offer -------------------------------------------

  /** The mowers the schedule starts: the mower, or every member of the group. */
  const members = $derived.by((): string[] => {
    if (!form.targetId) return [];
    if (form.targetType === 'mower') return [form.targetId];
    return [...(groups.find((g) => g.id === form.targetId)?.related ?? [])];
  });
  const memberRows = $derived(
    members.map((id) => {
      const caps = capabilities?.[id] ?? null;
      return {
        id,
        name: mowerNames[id] ?? mowers.find((x) => x.id === id)?.name ?? id.slice(0, 8),
        model: caps ? displayModel(caps, null) : null,
        known: caps !== null,
      };
    }),
  );
  /** One mower: what it declares; a group: what every member declares. */
  const offer = $derived(
    capabilities === null ? NO_OFFER : commonOffer(members.map((id) => capabilities[id])),
  );
  const mowParts = $derived(mowSummary({ ...mowBody(form.mow), rotation_angles: form.rotation }));

  // Another target: what it does not offer is cleared (never moved into its range).
  let fittedFor = untrack(() => `${form.targetType}:${form.targetId}`);
  $effect(() => {
    const key = `${form.targetType}:${form.targetId}`;
    if (capabilities === null || key === fittedFor) return;
    fittedFor = key;
    const fitted = fitMowDraft(
      untrack(() => form.mow),
      offer,
    );
    form.mow = fitted;
  });

  // --- the rotation list -----------------------------------------------------------------------

  let newAngle = $state('');
  const rotationPresets: { label: string; angles: number[] }[] = [
    { label: '0° / 90°', angles: [0, 90] },
    { label: '0° / 45° / 90° / 135°', angles: [0, 45, 90, 135] },
  ];

  function addAngle(): void {
    const value = parseNumber(newAngle);
    if (value === null || value < -360 || value > 360) return;
    form.rotation = [...form.rotation, value];
    newAngle = '';
  }

  function removeAngle(index: number): void {
    form.rotation = form.rotation.filter((_, i) => i !== index);
  }

  function moveAngle(index: number, delta: -1 | 1): void {
    const next = [...form.rotation];
    const target = index + delta;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target]!, next[index]!];
    form.rotation = next;
  }

  const angleValid = $derived.by(() => {
    const value = parseNumber(newAngle);
    return value !== null && value >= -360 && value <= 360;
  });

  // --- time zone -------------------------------------------------------------------------------

  const deviceZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

  // --- issues and submit -----------------------------------------------------------------------

  function issueText(issue: FormIssue | undefined): string | null {
    if (!issue) return null;
    switch (issue.code) {
      case 'required':
        return m.sched_err_required();
      case 'days':
        return m.sched_err_days();
      case 'format':
        return m.sched_err_format();
      case 'integer':
        return m.sched_err_integer();
      case 'number':
        return m.sched_err_number();
      case 'range':
        return m.sched_err_range({
          min: formatNumber(issue.min, issue.min % 1 ? 1 : 0),
          max: formatNumber(issue.max, issue.max % 1 ? 1 : 0),
        });
      case 'count':
        return m.sched_err_count({ max: issue.max });
      case 'timezone':
        return m.sched_err_timezone();
      case 'group_areas':
        return m.sched_err_group_areas();
      case 'server':
        return issue.message;
    }
  }

  const err = (field: FormField) => issueText(issues[field]);
  const INVALID = '[aria-invalid="true"], [data-invalid="true"] button';

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    tried = true;
    if (Object.keys(validateForm(form)).length) {
      await tick();
      document.querySelector<HTMLElement>(INVALID)?.focus();
      return;
    }
    onsubmit(form);
  }

  // Focus the first field the server refused.
  $effect(() => {
    if (Object.keys(server).length) {
      void tick().then(() => document.querySelector<HTMLElement>(INVALID)?.focus());
    }
  });
</script>

<form class="form" onsubmit={submit} novalidate data-testid="schedule-form">
  <Panel label={m.sched_section_general()}>
    <div class="fields">
      <Field
        id="sched-name"
        label={m.sched_name()}
        required
        maxlength={LIMITS.name}
        placeholder={m.sched_name_placeholder()}
        bind:value={form.name}
        error={err('name')}
      />
      <div class="target" class:split={targetKinds.length > 1}>
        {#if targetKinds.length > 1}
          <SegmentedControl
            label={m.sched_target()}
            options={targetKinds}
            value={form.targetType}
            onchange={setTargetType}
            testid="sched-target-kind"
          />
        {/if}
        <Select
          id="sched-target"
          label={form.targetType === 'group' ? m.sched_target_group() : m.sched_target_mower()}
          options={targetOptions}
          bind:value={form.targetId}
          hint={err('target') ?? undefined}
        />
      </div>
      <Switch
        id="sched-enabled"
        testid="sched-enabled"
        label={m.sched_enabled()}
        hint={m.sched_enabled_hint()}
        states={switchStates}
        bind:checked={form.enabled}
      />
    </div>
  </Panel>

  <Panel label={m.sched_section_when()}>
    <div class="fields">
      <SegmentedControl
        label={m.sched_kind()}
        options={kindOptions}
        value={form.kind}
        onchange={(value) => (form.kind = value === 'every_n_days' ? 'every_n_days' : 'weekly')}
        testid="sched-kind"
      />
      {#if form.kind === 'weekly'}
        <fieldset class="days" data-invalid={issues.days ? 'true' : undefined}>
          <legend>{m.sched_days()}</legend>
          <div class="chips" data-testid="sched-days">
            {#each WEEKDAYS as day (day)}
              <button
                type="button"
                class="day"
                aria-pressed={form.days.includes(day)}
                data-day={day}
                onclick={() => toggleDay(day)}>{weekdayShort(day, locale)}</button
              >
            {/each}
          </div>
          <div class="presets">
            <button type="button" class="preset" onclick={() => setDays(WEEKDAYS.slice(0, 5))}
              >{m.sched_preset_workdays()}</button
            >
            <button type="button" class="preset" onclick={() => setDays(['sat', 'sun'])}
              >{m.sched_preset_weekend()}</button
            >
            <button type="button" class="preset" onclick={() => setDays(WEEKDAYS)}
              >{m.sched_daily()}</button
            >
          </div>
          {#if err('days')}<span class="error" role="alert">{err('days')}</span>{/if}
        </fieldset>
      {:else}
        <div class="pair">
          <Field
            id="sched-anchor"
            type="date"
            label={m.sched_anchor_date()}
            hint={m.sched_anchor_hint()}
            mono
            bind:value={form.anchorDate}
            error={err('anchor_date')}
          />
          <Field
            id="sched-interval"
            label={m.sched_interval_days()}
            inputmode="numeric"
            mono
            bind:value={form.intervalDays}
            error={err('interval_days')}
          />
        </div>
      {/if}
      <div class="pair">
        <Field
          id="sched-time"
          type="time"
          label={m.sched_start_time()}
          mono
          bind:value={form.startTime}
          error={err('start_time')}
        />
        <Field
          id="sched-duration"
          label={m.sched_duration()}
          inputmode="numeric"
          mono
          hint={durationPreview ? m.sched_duration_hint({ duration: durationPreview }) : undefined}
          bind:value={form.duration}
          error={err('duration')}
        />
      </div>
      <Switch
        id="sched-window-dock"
        label={m.sched_return_at_end()}
        hint={m.sched_return_at_end_hint()}
        states={switchStates}
        bind:checked={form.returnAtWindowEnd}
      />
      <div class="tz">
        <Field
          id="sched-timezone"
          label={m.sched_timezone()}
          mono
          placeholder={siteTimeZone ?? undefined}
          hint={m.sched_timezone_hint({ zone: siteTimeZone ?? m.no_data() })}
          bind:value={form.timezone}
          error={err('timezone')}
        />
        {#if form.timezone !== deviceZone}
          <button type="button" class="preset" onclick={() => (form.timezone = deviceZone)}
            >{m.sched_timezone_device({ zone: deviceZone })}</button
          >
        {/if}
      </div>
    </div>
  </Panel>

  <Panel label={m.sched_section_areas()} testid="sched-areas">
    {#if form.targetType === 'group'}
      <p class="hint">{m.sched_areas_group()}</p>
    {:else if !form.targetId}
      <p class="hint">{m.sched_areas_no_target()}</p>
    {:else if mapState === 'loading'}
      <p class="hint" role="status"><SlowLoading /></p>
    {:else if mapState === 'none' || !areas.length}
      <p class="hint">{m.sched_areas_no_map()}</p>
    {:else if mapSnapshot}
      <p class="hint">{m.sched_areas_hint()}</p>
      <div class="map">
        {#if !mapFailed}
          <MowerMap
            map={mapSnapshot}
            selectable
            selectedAreas={form.areaIds}
            {tiles}
            {labels}
            {areaName}
            onselectionchange={(ids) => (form.areaIds = ids)}
            onfail={() => (mapFailed = true)}
            testid="sched-map"
          />
        {:else}
          <AreaPlan map={mapSnapshot} selected={form.areaIds} label={m.detail_areas()} {areaName} />
        {/if}
      </div>
      <ul class="area-list">
        {#each areas as area (area.id)}
          {@const order = form.areaIds.indexOf(area.id)}
          <li>
            <button
              type="button"
              class="area"
              aria-pressed={order >= 0}
              data-area={area.id}
              onclick={() => toggleArea(area.id)}
            >
              <span class="order mono">{order >= 0 ? order + 1 : ''}</span>
              <span>{areaName(area)}</span>
            </button>
          </li>
        {/each}
      </ul>
      <p class="readout mono" data-testid="sched-area-summary">
        {form.areaIds.length
          ? m.sched_areas_chosen({ count: form.areaIds.length })
          : m.sched_areas_all()}
      </p>
    {/if}
    {#if err('areas')}<span class="error" role="alert">{err('areas')}</span>{/if}
  </Panel>

  <Panel label={m.sched_section_pattern()} testid="sched-mow">
    <div class="fields">
      <div class="applies" data-testid="sched-mow-members">
        <span class="label"
          >{form.targetType === 'group'
            ? m.sched_mow_applies_group({ count: members.length })
            : m.sched_mow_applies_mower()}</span
        >
        {#if memberRows.length}
          <ul class="members">
            {#each memberRows as row (row.id)}
              <li data-mower={row.id} data-known={row.known}>
                <span class="member-name">{row.name}</span>
                <span class="member-model mono"
                  >{row.known ? (row.model ?? m.no_data()) : m.sched_mow_caps_unknown()}</span
                >
              </li>
            {/each}
          </ul>
        {/if}
        {#if form.targetType === 'group' && members.length > 1}
          <p class="hint small">{m.sched_mow_common_hint()}</p>
        {/if}
      </div>
      {#if capabilities === null}
        <p class="hint" role="status"><SlowLoading /></p>
      {:else if offersAnything(offer)}
        <p class="mow-summary mono" data-testid="sched-mow-summary">
          {mowParts.length ? mowParts.join(' · ') : m.sched_mow_defaults()}
        </p>
        <div class="mow-options" data-invalid={issues.mow ? 'true' : undefined}>
          <MowOptions
            unset
            prefix="sched-mow"
            offered={offer.offered}
            spacing={offer.spacing}
            options={offer.options}
            bladeRange={offer.blade}
            speedRange={offer.speed}
            bind:draft={form.mow.pattern}
            bind:setpoints={form.mow.setpoints}
          />
        </div>
        <p class="hint small">{m.sched_mow_hint()}</p>
      {:else}
        <p class="hint" data-testid="sched-mow-none">{m.sched_mow_none_offered()}</p>
      {/if}
      {#if err('mow')}<span class="error" role="alert">{err('mow')}</span>{/if}
      {#if offer.offered.length}
        <div class="rotation" data-invalid={issues.rotation ? 'true' : undefined}>
          <span class="label">{m.sched_rotation()}</span>
          <p class="hint small">{m.sched_rotation_hint()}</p>
          {#if form.rotation.length}
            <ol class="angles" data-testid="sched-rotation">
              {#each form.rotation as angle, i (i)}
                <li>
                  <span class="mono step">{i + 1}</span>
                  <span class="mono value">{formatNumber(angle, angle % 1 ? 1 : 0)}°</span>
                  <button
                    type="button"
                    class="mini turn"
                    aria-label={m.sched_rotation_up()}
                    disabled={i === 0}
                    onclick={() => moveAngle(i, -1)}><Icon name="back" size={16} /></button
                  >
                  <button
                    type="button"
                    class="mini turn"
                    aria-label={m.sched_rotation_down()}
                    disabled={i === form.rotation.length - 1}
                    onclick={() => moveAngle(i, 1)}><Icon name="chevron" size={16} /></button
                  >
                  <button
                    type="button"
                    class="mini"
                    aria-label={m.remove()}
                    onclick={() => removeAngle(i)}><Icon name="close" size={16} /></button
                  >
                </li>
              {/each}
            </ol>
          {/if}
          <div class="add">
            <Field
              id="sched-rotation-new"
              label={m.sched_rotation_add_label()}
              inputmode="decimal"
              mono
              bind:value={newAngle}
            />
            <Button
              icon="plus"
              disabled={!angleValid || form.rotation.length >= LIMITS.rotation}
              testid="sched-rotation-add"
              onclick={addAngle}>{m.sched_rotation_add()}</Button
            >
          </div>
          <div class="presets">
            {#each rotationPresets as preset (preset.label)}
              <button
                type="button"
                class="preset mono"
                onclick={() => (form.rotation = [...preset.angles])}>{preset.label}</button
              >
            {/each}
            {#if form.rotation.length}
              <button type="button" class="preset" onclick={() => (form.rotation = [])}
                >{m.sched_rotation_clear()}</button
              >
            {/if}
          </div>
          {#if err('rotation')}<span class="error" role="alert">{err('rotation')}</span>{/if}
        </div>
      {/if}
    </div>
  </Panel>

  <Panel label={m.sched_section_rain()} testid="sched-rain">
    <div class="fields">
      <Switch
        id="sched-rain-enabled"
        testid="sched-rain-enabled"
        label={m.sched_rain_enabled()}
        hint={m.sched_rain_enabled_hint()}
        states={switchStates}
        bind:checked={form.rainEnabled}
      />
      {#if form.rainEnabled}
        <div class="quad">
          <Field
            id="sched-rain-probability"
            label={m.sched_rain_probability()}
            inputmode="numeric"
            mono
            hint="%"
            bind:value={form.probability}
            error={err('probability')}
          />
          <Field
            id="sched-rain-mm"
            label={m.sched_rain_mm()}
            inputmode="decimal"
            mono
            hint="mm/h"
            bind:value={form.precipitation}
            error={err('precipitation')}
          />
          <Field
            id="sched-rain-lookahead"
            label={m.sched_rain_lookahead()}
            inputmode="numeric"
            mono
            hint="h"
            bind:value={form.lookahead}
            error={err('lookahead')}
          />
          <Field
            id="sched-rain-cooldown"
            label={m.sched_rain_cooldown()}
            inputmode="numeric"
            mono
            hint="h"
            bind:value={form.cooldown}
            error={err('cooldown')}
          />
        </div>
        <Switch
          id="sched-rain-unknown"
          label={m.sched_rain_unknown()}
          hint={m.sched_rain_unknown_hint()}
          states={switchStates}
          bind:checked={form.runWhenUnknown}
        />
        <Switch
          id="sched-rain-running"
          label={m.sched_rain_running()}
          hint={m.sched_rain_running_hint()}
          states={switchStates}
          bind:checked={form.pauseRunning}
        />
      {/if}
    </div>
  </Panel>

  <div class="actions">
    {#if problem}
      <ProblemNotice
        title={problemMessage(problem)}
        detail={problem.detail}
        reference={problem.correlationId}
        referenceLabel={m.problem_reference()}
      />
    {/if}
    <Button type="submit" variant="primary" icon="check" {pending} testid="sched-save"
      >{submitLabel}</Button
    >
    {@render footer?.()}
  </div>
</form>

<style>
  .form {
    min-width: 0;
  }

  .fields {
    display: grid;
    gap: var(--md-space-4);
  }

  .target {
    display: grid;
    gap: var(--md-space-3);
  }

  .pair,
  .quad {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--md-space-3);
  }

  .days {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
    margin: 0;
    padding: 0;
    border: 0;
  }

  legend,
  .label {
    margin-bottom: var(--md-space-1);
    padding: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    line-height: var(--md-line-16);
    text-transform: uppercase;
  }

  .chips {
    display: grid;
    grid-template-columns: repeat(7, minmax(0, 1fr));
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    overflow: hidden;
  }

  .days[data-invalid='true'] .chips {
    border-color: var(--md-state-error);
  }

  .day {
    min-height: var(--md-target);
    padding: 0;
    border: 0;
    border-radius: var(--md-radius-0);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-medium);
    cursor: pointer;
    transition: background-color var(--md-motion-fast) var(--md-ease);
  }

  .day + .day {
    border-inline-start: var(--md-hairline) solid var(--md-color-line);
  }

  .day[aria-pressed='true'] {
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .day:focus-visible,
  .preset:focus-visible,
  .area:focus-visible,
  .mini:focus-visible {
    position: relative;
    z-index: 1;
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .presets {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
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

  .preset.mono {
    font-family: var(--md-font-mono);
    letter-spacing: 0;
    text-transform: none;
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

  .error {
    color: var(--md-state-error);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  .hint {
    margin: 0 0 var(--md-space-3);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .hint.small {
    margin-bottom: var(--md-space-1);
  }

  .map {
    height: 300px;
    border: var(--md-hairline) solid var(--md-color-line);
  }

  .area-list {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
    margin: var(--md-space-3) 0 0;
    padding: 0;
    list-style: none;
  }

  .area {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-2);
    min-height: var(--md-target);
    padding: 0 var(--md-space-3) 0 var(--md-space-2);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    cursor: pointer;
  }

  .area .order {
    display: grid;
    place-items: center;
    width: 24px;
    height: 24px;
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
    font-size: var(--md-font-size-12);
  }

  .area[aria-pressed='true'] {
    border-color: var(--md-color-focus);
  }

  .area[aria-pressed='true'] .order {
    border-color: var(--md-color-focus);
    background: var(--md-color-focus);
    color: var(--md-color-ink-inverse);
  }

  .readout {
    margin: var(--md-space-3) 0 0;
    font-size: var(--md-font-size-14);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .members {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .members li {
    display: inline-flex;
    align-items: baseline;
    gap: var(--md-space-2);
    padding: var(--md-space-1) var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
    font-size: var(--md-font-size-14);
  }

  .members li[data-known='false'] {
    border-style: dashed;
  }

  .member-name {
    font-weight: var(--md-weight-medium);
  }

  .member-model {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .applies {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
  }

  .mow-summary {
    margin: 0;
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 3px solid var(--md-color-ink);
    background: var(--md-color-surface-sunken);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
    font-weight: var(--md-weight-medium);
  }

  .mow-options {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-3);
  }

  .rotation {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
  }

  .angles {
    margin: 0;
    padding: 0;
    list-style: none;
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .angles li {
    display: grid;
    grid-template-columns: 28px minmax(0, 1fr) repeat(3, var(--md-target));
    align-items: center;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .step {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .value {
    font-size: var(--md-font-size-20);
    font-weight: var(--md-weight-medium);
  }

  .mini {
    display: grid;
    place-items: center;
    width: var(--md-target);
    height: var(--md-target);
    padding: 0;
    border: 0;
    border-radius: var(--md-radius-0);
    background: none;
    color: var(--md-color-ink);
    cursor: pointer;
  }

  .turn :global(svg) {
    transform: rotate(90deg);
  }

  .mini:disabled {
    color: var(--md-color-line-strong);
    cursor: not-allowed;
  }

  .add {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: end;
    gap: var(--md-space-2);
  }

  .actions {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--md-space-3);
    padding: var(--md-space-2) var(--md-gutter) var(--md-space-6);
  }

  @media (min-width: 768px) {
    .target.split {
      grid-template-columns: auto minmax(0, 1fr);
      align-items: end;
    }

    .quad {
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .day {
      transition: none;
    }
  }
</style>

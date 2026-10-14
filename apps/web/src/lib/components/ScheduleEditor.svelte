<!--
  The page body of `/schedules/new` and `/schedules/[id]`: loads the schedule, the targets and
  the rights, shows the form with the preview beside it (the rain verdict, the forecast strip,
  the next firings), saves with POST or PATCH and deletes. Without the right to edit the target
  the schedule is shown read-only (summary and preview); the Scheduler decides in every case.
-->
<script lang="ts">
  import { Button, Panel, ProblemNotice, Readout, TopBar } from '@mowdeck/ui';

  import { goto } from '$app/navigation';
  import { page } from '$app/state';

  import { schedulerApi, unwrap } from '$lib/api/client';
  import { asProblem, type ApiProblem } from '$lib/api/problem';
  import ScheduleForm from '$lib/components/ScheduleForm.svelte';
  import SchedulePreview from '$lib/components/SchedulePreview.svelte';
  import SlowLoading from '$lib/components/SlowLoading.svelte';
  import { loadConfig, type MapTilesConfig } from '$lib/config';
  import { problemMessage } from '$lib/i18n';
  import { m } from '$lib/paraglide/messages';
  import { getLocale } from '$lib/paraglide/runtime';
  import { notifyPlanProblem } from '$lib/plan/problems';
  import {
    emptyForm,
    fromSchedule,
    localDate,
    toBody,
    type ScheduleFormState,
  } from '$lib/schedules/form';
  import { recurrenceSummary } from '$lib/schedules/recurrence';
  import {
    loadPreview,
    loadTargets,
    siteTimeZone,
    targetRights,
    type Targets,
  } from '$lib/schedules/runtime';
  import type { Locale, Preview, RainPause, Schedule } from '$lib/schedules/types';
  import { toasts } from '$lib/stores/ui.svelte';

  let { id }: { id: string | null } = $props();

  let schedule = $state<Schedule | null>(null);
  let targets = $state<Targets>({ mowers: [], groups: [], sites: [] });
  let form = $state<ScheduleFormState | null>(null);
  let loadProblem = $state<ApiProblem | null>(null);
  let saveProblem = $state<ApiProblem | null>(null);
  let saving = $state(false);
  let canEdit = $state<boolean | null>(null);
  let tiles = $state.raw<MapTilesConfig | null>(null);
  let preview = $state<Preview | null>(null);
  let previewProblem = $state<ApiProblem | null>(null);
  let confirmDelete = $state(false);

  const locale = $derived(getLocale() as Locale);
  const editable = $derived(
    form !== null && (id === null ? targets.mowers.length + targets.groups.length > 0 : canEdit),
  );
  const zone = $derived(form ? siteTimeZone(targets, form.targetType, form.targetId) : null);
  const dirty = $derived(
    !!schedule &&
      !!form &&
      JSON.stringify(toBody(form, schedule.pattern)) !==
        JSON.stringify(toBody(fromSchedule(schedule), schedule.pattern)),
  );
  const rain = $derived<RainPause>(
    schedule?.rain_pause ?? {
      enabled: true,
      probability_threshold: 60,
      precipitation_mm_threshold: 0.5,
      lookahead_hours: 3,
      cooldown_hours: 2,
      run_when_unknown: true,
    },
  );

  $effect(() => {
    void loadConfig().then((config) => (tiles = config.mapTiles));
  });

  $effect(() => {
    const current = id;
    void load(current);
  });

  async function load(current: string | null): Promise<void> {
    loadProblem = null;
    try {
      targets = await loadTargets();
      if (current) {
        schedule = await unwrap(
          schedulerApi.GET('/api/v1/scheduler/schedules/{schedule_id}', {
            params: { path: { schedule_id: current } },
          }),
        );
        form = fromSchedule(schedule);
        const type = schedule.target_type === 'group' ? 'group' : 'mower';
        const found = await targetRights([{ type, id: schedule.target_id }]);
        canEdit = found[`${type}:${schedule.target_id}`]?.edit ?? false;
        void refreshPreview(current);
      } else {
        // Offer only the targets the user may create schedules for (the API decides again).
        const found = await targetRights([
          ...targets.mowers.map((x) => ({ type: 'mower' as const, id: x.id })),
          ...targets.groups.map((g) => ({ type: 'group' as const, id: g.id })),
        ]);
        targets = {
          ...targets,
          mowers: targets.mowers.filter((x) => found[`mower:${x.id}`]?.edit),
          groups: targets.groups.filter((g) => found[`group:${g.id}`]?.edit),
        };
        // `?group=<id>` (from the fleet's group page) or `?mower=<id>` preselects the target.
        const wantedGroup = page.url.searchParams.get('group');
        const group = targets.groups.find((g) => g.id === wantedGroup);
        const wanted = page.url.searchParams.get('mower');
        const mower = targets.mowers.find((x) => x.id === wanted) ?? targets.mowers[0];
        form = emptyForm(
          localDate(new Date()),
          group
            ? { type: 'group', id: group.id }
            : mower
              ? { type: 'mower', id: mower.id }
              : undefined,
        );
        canEdit = true;
      }
    } catch (error) {
      loadProblem = asProblem(error);
    }
  }

  async function refreshPreview(current: string): Promise<void> {
    previewProblem = null;
    try {
      preview = await loadPreview(current, 5);
    } catch (error) {
      previewProblem = asProblem(error);
    }
  }

  // A new schedule has no preview yet: show the current rain decision at the target mower.
  $effect(() => {
    if (id !== null || !form || form.targetType !== 'mower' || !form.targetId) return;
    const mowerId = form.targetId;
    let cancelled = false;
    void unwrap(
      schedulerApi.GET('/api/v1/scheduler/weather/{mower_id}', {
        params: { path: { mower_id: mowerId } },
      }),
    )
      .then((answer) => {
        if (!cancelled)
          preview = { items: [], weather: answer.decision, weather_mower_id: mowerId };
      })
      .catch((error: unknown) => {
        if (!cancelled) previewProblem = asProblem(error);
      });
    return () => {
      cancelled = true;
    };
  });

  async function save(values: ScheduleFormState): Promise<void> {
    saving = true;
    saveProblem = null;
    try {
      if (id === null) {
        await unwrap(schedulerApi.POST('/api/v1/scheduler/schedules', { body: toBody(values) }));
        toasts.push('ok', m.sched_created());
        await goto('/schedules');
        return;
      }
      schedule = await unwrap(
        schedulerApi.PATCH('/api/v1/scheduler/schedules/{schedule_id}', {
          params: { path: { schedule_id: id } },
          body: toBody(values, schedule?.pattern),
        }),
      );
      form = fromSchedule(schedule);
      toasts.push('ok', m.saved());
      void refreshPreview(id);
    } catch (error) {
      saveProblem = asProblem(error);
      notifyPlanProblem(saveProblem);
    } finally {
      saving = false;
    }
  }

  async function remove(): Promise<void> {
    if (!id) return;
    if (!confirmDelete) {
      confirmDelete = true;
      return;
    }
    saving = true;
    try {
      await unwrap(
        schedulerApi.DELETE('/api/v1/scheduler/schedules/{schedule_id}', {
          params: { path: { schedule_id: id } },
        }),
      );
      toasts.push('ok', m.sched_deleted());
      await goto('/schedules');
    } catch (error) {
      saveProblem = asProblem(error);
      notifyPlanProblem(saveProblem);
    } finally {
      saving = false;
    }
  }

  function targetName(type: string, targetId: string): string {
    const list = type === 'group' ? targets.groups : targets.mowers;
    return list.find((t) => t.id === targetId)?.name ?? m.sched_target_removed();
  }
</script>

<svelte:head><title>{schedule?.name ?? m.sched_new_title()} · {m.app_name()}</title></svelte:head>

<TopBar
  title={id === null ? m.sched_new_title() : (schedule?.name ?? m.sched_title())}
  meta={schedule ? recurrenceSummary(schedule, locale) : undefined}
  backHref="/schedules"
  backLabel={m.back()}
>
  {#snippet actions()}
    {#if id}
      <Button variant="ghost" href="/schedules/{id}/runs" icon="list" testid="schedule-runs-link"
        >{m.sched_runs()}</Button
      >
    {/if}
  {/snippet}
</TopBar>

{#if loadProblem}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(loadProblem)}
      detail={loadProblem.detail}
      reference={loadProblem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if !form || canEdit === null}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{:else if id === null && !editable}
  <div class="pad">
    <ProblemNotice tone="info" title={m.sched_no_targets()} />
  </div>
{:else}
  <div class="columns">
    <div class="main">
      {#if editable}
        <ScheduleForm
          bind:form={() => form!, (value) => (form = value)}
          mowers={targets.mowers}
          groups={targets.groups}
          siteTimeZone={zone}
          {tiles}
          pending={saving}
          problem={saveProblem}
          submitLabel={id === null ? m.sched_create() : m.save()}
          onsubmit={save}
        >
          {#snippet footer()}
            {#if id}
              <div class="danger">
                <Button
                  variant="danger"
                  icon="trash"
                  pending={saving}
                  testid="schedule-delete"
                  onclick={remove}
                  >{confirmDelete ? m.sched_delete_confirm() : m.sched_delete()}</Button
                >
                {#if confirmDelete}
                  <Button variant="ghost" onclick={() => (confirmDelete = false)}
                    >{m.cancel()}</Button
                  >
                {/if}
              </div>
            {/if}
          {/snippet}
        </ScheduleForm>
      {:else if schedule}
        <div class="pad">
          <ProblemNotice tone="info" title={m.sched_read_only()} />
        </div>
        <Panel label={m.sched_section_general()} testid="schedule-readonly">
          <div class="readouts">
            <Readout
              label={m.sched_target()}
              value={targetName(schedule.target_type, schedule.target_id)}
              size="inline"
              emptyText={m.no_data()}
            />
            <Readout
              label={m.sched_kind()}
              value={recurrenceSummary(schedule, locale)}
              size="inline"
              emptyText={m.no_data()}
            />
            <Readout
              label={m.sched_timezone()}
              value={schedule.timezone}
              size="inline"
              emptyText={m.no_data()}
            />
            <Readout
              label={m.sched_section_areas()}
              value={schedule.area_ids.length
                ? m.sched_areas_chosen({ count: schedule.area_ids.length })
                : m.sched_areas_all()}
              size="inline"
              emptyText={m.no_data()}
            />
          </div>
        </Panel>
      {/if}
    </div>

    <aside class="aside" aria-label={m.sched_preview()}>
      <Panel label={m.sched_preview()} testid="schedule-preview-panel">
        {#if id === null && form.targetType !== 'mower'}
          <p class="quiet small">{m.sched_preview_after_save()}</p>
        {:else}
          {#if id === null}
            <p class="quiet small">{m.sched_preview_now()}</p>
          {/if}
          <SchedulePreview
            {preview}
            {rain}
            timeZone={schedule?.timezone ?? zone}
            problem={previewProblem}
            {dirty}
            firings={id !== null}
            testid="schedule-preview"
          />
        {/if}
      </Panel>
    </aside>
  </div>
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .quiet {
    margin: 0;
    color: var(--md-color-ink-muted);
  }

  .small {
    margin-bottom: var(--md-space-3);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .columns {
    display: grid;
    max-width: 760px;
  }

  .main {
    min-width: 0;
  }

  .readouts {
    display: grid;
    gap: var(--md-space-3);
  }

  .danger {
    display: flex;
    gap: var(--md-space-2);
    margin-top: var(--md-space-4);
  }

  @media (min-width: 1024px) {
    .columns {
      grid-template-columns: minmax(0, 1fr) var(--md-instrument-column);
      align-items: start;
      max-width: 1200px;
    }

    .aside {
      position: sticky;
      top: 0;
      border-inline-start: var(--md-hairline) solid var(--md-color-line);
    }
  }
</style>

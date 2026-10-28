<!--
  Health of one mower (Telemetry, ADR 0027): blade hours since the change against the change
  interval (the mower's own counter when it reports one, as the vendor app shows it; MowDeck's own
  counter otherwise, which then stays visible in the totals; the interval is the one effective
  threshold Telemetry names: set in MowDeck, else the mower's own warning, else the default), the
  running totals (mowing hours, distance and battery cycles from the mower's own lifetime counters
  when it reports them, MowDeck's counts beside them), the last 30 UTC days as bar strips, the battery of the last
  24 hours, errors by code and the blade changes. "Blades replaced" records a change; it needs
  `can_manage_mowers` (checked first, and hidden for good after a 403).
-->
<script lang="ts">
  import {
    Button,
    Field,
    Icon,
    Panel,
    ProblemNotice,
    Readout,
    Sheet,
    TopBar,
    type IconName,
  } from '@mowdeck/ui';

  import { page } from '$app/state';

  import { fleetApi, telemetryApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import BatteryTrace from '#lib/components/BatteryTrace.svelte';
  import DailyBars from '#lib/components/DailyBars.svelte';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import StopBar from '#lib/components/StopBar.svelte';
  import type { components as Fleet } from '#lib/generated/fleet.js';
  import {
    batteryExtremes,
    batteryQuery,
    bladeMeter,
    cycleCount,
    dayRange,
    durationParts,
    errorRows,
    fillDays,
    lifetimeTotals,
    thresholdLevel,
    thresholdShare,
    type BatteryPoint,
    type HealthTotals,
    type ThresholdLevel,
  } from '#lib/health/series.js';
  import { formatDateTime, formatNumber, problemMessage } from '#lib/i18n.js';
  import { permissions, revoked } from '#lib/live.svelte.js';
  import { m } from '#lib/paraglide/messages.js';
  import { getLocale } from '#lib/paraglide/runtime.js';
  import { toasts } from '#lib/stores/ui.svelte.js';
  import type { components as Telemetry } from '#lib/generated/telemetry.js';

  type Mower = Fleet['schemas']['MowerOut'];
  type BladeChange = Telemetry['schemas']['BladeChangeOut'];
  type HealthDay = Telemetry['schemas']['HealthDayOut'];

  const id = $derived(page.params.id ?? '');

  let mower = $state<Mower | null>(null);
  let notFound = $state(false);
  let problem = $state<ApiProblem | null>(null);
  let totals = $state<HealthTotals | null>(null);
  let noData = $state(false);
  let days = $state.raw<HealthDay[]>([]);
  let battery = $state.raw<BatteryPoint[]>([]);
  let batteryWindow = $state({ from: 0, to: 0 });
  let changes = $state.raw<BladeChange[]>([]);
  let canManage = $state(false);
  let resetOpen = $state(false);
  let note = $state('');
  let saving = $state(false);
  let resetProblem = $state<ApiProblem | null>(null);
  let barHeight = $state(120);
  let today = $state(new Date());

  const path = (mowerId: string) => ({ params: { path: { mower_id: mowerId } } });

  async function loadTotals(mowerId: string): Promise<void> {
    try {
      totals = await unwrap(
        telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/health', path(mowerId)),
      );
      noData = false;
    } catch (error) {
      const p = asProblem(error);
      if (p.status === 404) noData = true;
      else problem = p;
    }
  }

  async function loadChanges(mowerId: string): Promise<void> {
    try {
      const list = await unwrap(
        telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/health/blade-changes', path(mowerId)),
      );
      changes = list.items;
    } catch {
      changes = [];
    }
  }

  async function load(mowerId: string): Promise<void> {
    mower = null;
    notFound = false;
    problem = null;
    totals = null;
    noData = false;
    canManage = false;
    try {
      mower = await unwrap(fleetApi.GET('/api/v1/fleet/mowers/{mower_id}', path(mowerId)));
    } catch (error) {
      const p = asProblem(error);
      if (p.status === 404) notFound = true;
      else problem = p;
      return;
    }
    const now = new Date();
    today = now;
    const range = dayRange(now);
    const query = batteryQuery(now);
    batteryWindow = { from: Date.parse(query.from), to: Date.parse(query.to) };
    void loadTotals(mowerId);
    void loadChanges(mowerId);
    void unwrap(
      telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/health/daily', {
        params: { path: { mower_id: mowerId }, query: range },
      }),
    )
      .then((result) => (days = result.days))
      .catch(() => (days = []));
    void unwrap(
      telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/battery', {
        params: { path: { mower_id: mowerId }, query },
      }),
    )
      .then((result) => (battery = result.points))
      .catch(() => (battery = []));
    void permissions
      .check(mowerId, ['can_manage_mowers'])
      .then((result) => (canManage = result.can_manage_mowers))
      .catch(() => (canManage = false));
  }

  $effect(() => {
    void load(id);
  });

  async function saveReset(): Promise<void> {
    saving = true;
    resetProblem = null;
    try {
      totals = await unwrap(
        telemetryApi.POST('/api/v1/telemetry/mowers/{mower_id}/health/blade-reset', {
          params: { path: { mower_id: id } },
          body: { note: note.trim() || null },
        }),
      );
      noData = false;
      resetOpen = false;
      note = '';
      toasts.push('ok', m.health_blade_saved());
      void loadChanges(id);
    } catch (error) {
      const p = asProblem(error);
      if (p.status === 403) {
        canManage = false;
        resetOpen = false;
      }
      resetProblem = p;
    } finally {
      saving = false;
    }
  }

  // --- view model -------------------------------------------------------------------------------

  const locale = $derived(getLocale());
  const filled = $derived(fillDays(days, today));
  const dayKeys = $derived(filled.map((d) => d.day));
  const meter = $derived(totals ? bladeMeter(totals) : null);
  const cycles = $derived(totals ? cycleCount(totals) : null);
  const level = $derived<ThresholdLevel>(
    meter ? thresholdLevel(meter.hours, meter.threshold) : 'ok',
  );
  const share = $derived(meter ? thresholdShare(meter.hours, meter.threshold) : 0);
  const LEVELS: Record<ThresholdLevel, { word: () => string; icon: IconName }> = {
    ok: { word: () => m.health_threshold_ok(), icon: 'check' },
    soon: { word: () => m.health_threshold_soon(), icon: 'clock' },
    due: { word: () => m.health_threshold_due(), icon: 'alert' },
  };
  const extremes = $derived(batteryExtremes(battery));
  const errors = $derived(totals ? errorRows(totals.errors) : []);
  const lifetime = $derived(totals ? lifetimeTotals(totals) : null);

  /** A distance readout: metres below one kilometre, kilometres above. */
  function distance(metres: number): { text: string; unit: string } {
    return metres >= 1000
      ? { text: formatNumber(metres / 1000, 2), unit: 'km' }
      : { text: formatNumber(metres), unit: 'm' };
  }

  function hours(value: number): string {
    return formatNumber(value, value < 10 ? 2 : 1);
  }

  /** A duration readout: minutes below one hour, hours above. */
  function duration(value: number): { text: string; unit: string } {
    const parts = durationParts(value);
    return { text: formatNumber(parts.value, parts.digits), unit: parts.unit };
  }

  // The daily blade strip in minutes while no day reaches an hour.
  const dailyInMinutes = $derived(filled.every((d) => d.bladeHours < 1));
  const dailyBlade = $derived(
    filled.map((d) => (dailyInMinutes ? d.bladeHours * 60 : d.bladeHours)),
  );
  const dailyUnit = $derived(dailyInMinutes ? 'min' : 'h');
  const dailyDigits = $derived(dailyInMinutes ? (Math.max(0, ...dailyBlade) < 10 ? 1 : 0) : 2);
  const formatDaily = (value: number) => formatNumber(value, dailyDigits);

  function shortDay(day: string): string {
    return new Intl.DateTimeFormat(locale, {
      day: '2-digit',
      month: '2-digit',
      timeZone: 'UTC',
    }).format(new Date(`${day}T00:00:00Z`));
  }

  function shortTime(at: number): string {
    return new Intl.DateTimeFormat(locale, { timeStyle: 'short' }).format(new Date(at));
  }

  function errorLabel(code: string): string {
    return code === 'unknown' ? m.health_errors_unknown() : code;
  }
</script>

<svelte:head><title>{m.health_title()} · {mower?.name ?? m.app_name()}</title></svelte:head>

{#if notFound || revoked.has(id)}
  <TopBar title={m.detail_not_found_title()} backHref="/mowers" backLabel={m.back()} />
  <div class="pad"><ProblemNotice tone="info" title={m.detail_not_found_title()} /></div>
{:else if problem}
  <TopBar title={m.health_title()} backHref="/mowers/{id}" backLabel={m.back()} />
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if mower}
  <div class="health" style:--bar-height="{barHeight}px" data-testid="health-page">
    <TopBar
      title={m.health_title()}
      meta={mower.name}
      backHref="/mowers/{id}"
      backLabel={m.back()}
    />

    {#if noData && !totals}
      <div class="pad" data-testid="health-empty">
        <ProblemNotice tone="info" title={m.health_no_data()} />
      </div>
    {/if}

    <div class="grid">
      <section class="hero" data-testid="health-threshold" data-level={level}>
        <div class="hero-head">
          <span class="label">{m.health_blade_since()}</span>
          <span class="level" data-level={level}>
            <Icon name={LEVELS[level].icon} size={16} />
            {LEVELS[level].word()}
          </span>
        </div>
        <div class="hero-value mono" data-testid="health-blade-since">
          {#if meter}
            {@const since = duration(meter.hours)}
            {since.text}<small>{since.unit}</small>
          {:else}
            —
          {/if}
        </div>
        <div
          class="meter"
          role="meter"
          aria-label={m.health_threshold()}
          aria-valuemin={0}
          aria-valuemax={meter?.threshold ?? 0}
          aria-valuenow={meter?.hours ?? 0}
        >
          <span class="fill" style:inline-size="{share * 100}%"></span>
          <span class="tick"></span>
        </div>
        <div class="hero-foot mono">
          <span>
            {m.health_threshold()}:
            {meter
              ? m.health_threshold_value({
                  hours: hours(meter.hours),
                  threshold: formatNumber(meter.threshold),
                })
              : '—'}
          </span>
          {#if meter?.source === 'device'}
            <span data-testid="health-blade-source" data-threshold-source={meter.thresholdSource}
              >{meter.thresholdSource === 'custom'
                ? m.health_blade_source_device_custom()
                : m.health_blade_source_device()}</span
            >
          {:else}
            <span>
              {totals?.blade_changed_at
                ? m.health_threshold_changed({ date: formatDateTime(totals.blade_changed_at) })
                : m.health_threshold_never()}
            </span>
          {/if}
        </div>
        {#if canManage}
          <div class="hero-action">
            <Button icon="blade" testid="blade-reset" onclick={() => (resetOpen = true)}
              >{m.health_blade_reset()}</Button
            >
          </div>
        {/if}
      </section>

      <Panel label={m.health_totals()} testid="health-totals">
        <div class="readouts">
          <Readout
            label={m.health_blade_total()}
            value={totals ? duration(totals.blade_hours).text : null}
            unit={totals ? duration(totals.blade_hours).unit : 'h'}
            emptyText={m.no_data()}
            testid="health-blade-hours"
          />
          {#if meter?.source === 'device' && totals}
            <Readout
              label={m.health_blade_since_mowdeck()}
              value={duration(totals.blade_hours_since_change).text}
              unit={duration(totals.blade_hours_since_change).unit}
              emptyText={m.no_data()}
              testid="health-blade-since-mowdeck"
            />
          {/if}
          <Readout
            label={lifetime?.mowingSource === 'device'
              ? m.health_mowing_device()
              : m.health_mowing()}
            value={lifetime ? duration(lifetime.mowingHours).text : null}
            unit={lifetime ? duration(lifetime.mowingHours).unit : 'h'}
            emptyText={m.no_data()}
            testid="health-mowing-hours"
          />
          {#if lifetime?.mowingSource === 'device'}
            <Readout
              label={m.health_mowing_mowdeck()}
              value={duration(lifetime.trackedHours).text}
              unit={duration(lifetime.trackedHours).unit}
              emptyText={m.no_data()}
              testid="health-mowing-hours-mowdeck"
            />
          {/if}
          <Readout
            label={lifetime?.distanceSource === 'device'
              ? m.health_distance_device()
              : m.health_distance()}
            value={lifetime ? distance(lifetime.distanceM).text : null}
            unit={lifetime ? distance(lifetime.distanceM).unit : 'm'}
            emptyText={m.no_data()}
            testid="health-distance"
          />
          {#if lifetime?.distanceSource === 'device'}
            <Readout
              label={m.health_distance_mowdeck()}
              value={distance(lifetime.trackedDistanceM).text}
              unit={distance(lifetime.trackedDistanceM).unit}
              emptyText={m.no_data()}
              testid="health-distance-mowdeck"
            />
          {/if}
          <Readout
            label={cycles?.source === 'device' ? m.health_cycles_device() : m.health_cycles()}
            value={cycles ? formatNumber(cycles.cycles) : null}
            emptyText={m.no_data()}
            testid="health-cycles"
          />
          {#if cycles?.source === 'device'}
            <Readout
              label={m.health_cycles_mowdeck()}
              value={formatNumber(cycles.tracked)}
              emptyText={m.no_data()}
              testid="health-cycles-mowdeck"
            />
          {/if}
          <Readout
            label={m.health_charges()}
            value={totals ? formatNumber(totals.charge_sessions) : null}
            emptyText={m.no_data()}
          />
        </div>
        {#if totals}
          <p class="hint mono">
            {totals.since
              ? `${m.health_since({ date: formatDateTime(totals.since) })} · `
              : ''}{m.health_updated({ time: formatDateTime(totals.updated_at) })}
          </p>
        {/if}
      </Panel>

      <Panel label={m.health_daily()} testid="health-daily">
        {#if filled.some((d) => d.bladeHours > 0 || d.distanceM > 0)}
          <div class="strips">
            <DailyBars
              label={m.health_daily_blade()}
              values={dailyBlade}
              days={dayKeys}
              format={formatDaily}
              unit={dailyUnit}
              peakLabel={(value) => m.health_daily_peak({ value })}
              summary={m.health_daily_label({
                metric: m.health_daily_blade(),
                days: filled.length,
                total: `${formatDaily(dailyBlade.reduce((sum, v) => sum + v, 0))} ${dailyUnit}`,
              })}
              firstLabel={shortDay(dayKeys[0] ?? '')}
              lastLabel={shortDay(dayKeys.at(-1) ?? '')}
              testid="daily-blade"
            />
            <DailyBars
              label={m.health_daily_distance()}
              values={filled.map((d) => d.distanceM)}
              days={dayKeys}
              format={(v) => formatNumber(v)}
              unit="m"
              peakLabel={(value) => m.health_daily_peak({ value })}
              summary={m.health_daily_label({
                metric: m.health_daily_distance(),
                days: filled.length,
                total: `${formatNumber(filled.reduce((s, d) => s + d.distanceM, 0))} m`,
              })}
              firstLabel={shortDay(dayKeys[0] ?? '')}
              lastLabel={shortDay(dayKeys.at(-1) ?? '')}
              testid="daily-distance"
            />
          </div>
        {:else}
          <p class="hint">{m.health_daily_empty()}</p>
        {/if}
      </Panel>

      <Panel label={m.health_battery()} testid="health-battery">
        {#if battery.length}
          <BatteryTrace
            points={battery}
            from={batteryWindow.from}
            to={batteryWindow.to}
            summary={m.health_battery_label({
              min: formatNumber(extremes?.min ?? 0),
              max: formatNumber(extremes?.max ?? 0),
            })}
            startLabel={shortTime(batteryWindow.from)}
            endLabel={shortTime(batteryWindow.to)}
            testid="battery-trace"
          />
          <div class="readouts three">
            <Readout
              label={m.health_battery_min()}
              value={extremes ? formatNumber(extremes.min) : null}
              unit="%"
              size="inline"
              tone={extremes && extremes.min < 20 ? 'error' : null}
              emptyText={m.no_data()}
            />
            <Readout
              label={m.health_battery_max()}
              value={extremes ? formatNumber(extremes.max) : null}
              unit="%"
              size="inline"
              emptyText={m.no_data()}
            />
            <Readout
              label={m.health_battery_now()}
              value={extremes ? formatNumber(extremes.last) : null}
              unit="%"
              size="inline"
              emptyText={m.no_data()}
            />
          </div>
        {:else}
          <p class="hint">{m.health_battery_empty()}</p>
        {/if}
      </Panel>

      <Panel label={m.health_errors()} testid="health-errors">
        {#if errors.length}
          <ul class="rows">
            {#each errors as row (row.code)}
              <li>
                <span class="code mono"><Icon name="alert" size={16} />{errorLabel(row.code)}</span>
                <span class="count mono">{formatNumber(row.count)}×</span>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="hint">{m.health_errors_none()}</p>
        {/if}
      </Panel>

      <Panel label={m.health_blade_changes()} testid="health-blade-changes">
        {#if changes.length}
          <ul class="rows">
            {#each changes as change (change.id)}
              <li>
                <span class="mono">{formatDateTime(change.changed_at)}</span>
                {#if change.note}<span class="note">{change.note}</span>{/if}
              </li>
            {/each}
          </ul>
        {:else}
          <p class="hint">{m.health_blade_changes_empty()}</p>
        {/if}
        {#if resetProblem && !resetOpen}
          <ProblemNotice
            title={problemMessage(resetProblem)}
            reference={resetProblem.correlationId}
            referenceLabel={m.problem_reference()}
          />
        {/if}
      </Panel>
    </div>

    <Sheet
      open={resetOpen}
      title={m.health_blade_reset_title()}
      closeLabel={m.close()}
      onclose={() => (resetOpen = false)}
      testid="blade-reset-sheet"
    >
      <p class="hint">{m.health_blade_reset_hint()}</p>
      {#if meter?.source === 'device'}
        <p class="hint" data-testid="blade-reset-device-hint">
          {m.health_blade_reset_device_hint()}
        </p>
      {/if}
      <Field
        id="blade-note"
        label={m.health_blade_note()}
        hint={m.health_blade_note_hint()}
        maxlength={500}
        bind:value={note}
        testid="blade-note"
      />
      {#if resetProblem}
        <ProblemNotice
          title={problemMessage(resetProblem)}
          reference={resetProblem.correlationId}
          referenceLabel={m.problem_reference()}
        />
      {/if}
      {#snippet footer()}
        <Button
          variant="primary"
          full
          icon="check"
          pending={saving}
          testid="blade-reset-save"
          onclick={saveReset}>{m.health_blade_save()}</Button
        >
      {/snippet}
    </Sheet>

    <StopBar mowerId={id} bind:height={barHeight} />
  </div>
{:else}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .quiet {
    color: var(--md-color-ink-muted);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .health {
    /* Sheets stop above the stop bar (phones: full width; desktops: its column). */
    --md-sheet-bottom: calc(var(--bar-height) + var(--md-space-2));

    padding-bottom: calc(var(--bar-height) + var(--md-space-4));
  }

  .grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 0;
  }

  .hero {
    display: grid;
    gap: var(--md-space-2);
    padding: var(--md-space-4) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  .hero-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-2);
  }

  .label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .level {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    padding: var(--md-space-0_5) var(--md-space-2);
    border: var(--md-border-control) solid var(--md-state-mowing);
    border-radius: var(--md-radius-1);
    background: var(--md-state-mowing-bg);
    color: var(--md-state-mowing);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .level[data-level='soon'] {
    border-color: var(--md-state-paused);
    background: var(--md-state-paused-bg);
    color: var(--md-state-paused);
  }

  .level[data-level='due'] {
    border-color: var(--md-state-error);
    background: var(--md-state-error-bg);
    color: var(--md-state-error);
  }

  .hero-value {
    color: var(--md-color-ink);
    font-size: var(--md-font-size-32);
    font-weight: var(--md-weight-medium);
    line-height: var(--md-line-32);
  }

  .hero-value small {
    margin-inline-start: var(--md-space-1);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-16);
  }

  .meter {
    position: relative;
    height: 12px;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface-sunken);
  }

  .fill {
    position: absolute;
    inset-block: 0;
    inset-inline-start: 0;
    background: var(--md-state-mowing);
  }

  [data-level='soon'] .fill {
    background: var(--md-state-paused);
  }

  [data-level='due'] .fill {
    background: var(--md-state-error);
  }

  .tick {
    position: absolute;
    inset-block: -3px;
    inset-inline-start: 80%;
    width: 0;
    border-inline-start: var(--md-border-control) dashed var(--md-color-ink);
  }

  .hero-foot {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: var(--md-space-1) var(--md-space-3);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .hero-action {
    display: flex;
    justify-content: flex-end;
  }

  .readouts {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--md-space-0_5);
  }

  .readouts.three {
    grid-template-columns: repeat(3, minmax(0, 1fr));
    margin-top: var(--md-space-2);
  }

  .strips {
    display: grid;
    gap: var(--md-space-4);
  }

  .hint {
    margin: var(--md-space-2) 0 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .rows {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .rows li {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-1) var(--md-space-3);
    min-height: 40px;
    padding: var(--md-space-1) 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    font-size: var(--md-font-size-14);
  }

  .code {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    min-width: 0;
    color: var(--md-state-error);
    overflow-wrap: anywhere;
  }

  .count {
    color: var(--md-color-ink);
  }

  .note {
    flex-basis: 100%;
    min-width: 0;
    color: var(--md-color-ink-muted);
    overflow-wrap: anywhere;
  }

  @media (min-width: 1024px) {
    .grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      align-items: start;
      max-width: 1100px;
      gap: var(--md-space-4);
      padding: var(--md-space-4) var(--md-gutter);
    }

    .hero {
      grid-column: 1 / -1;
      border: var(--md-hairline) solid var(--md-color-line);
    }

    .readouts {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
  }
</style>

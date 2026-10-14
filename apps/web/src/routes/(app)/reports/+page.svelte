<!--
  Reports per site (or the whole account) and period (today, 7 days, 30 days, custom; UTC days
  as Telemetry counts them): mowing hours, blade hours, distance, jobs, faults by code and
  availability (share of the elapsed period online), per mower and in total, with the fleet's
  daily mowing hours as a bar strip. One call (`GET /telemetry/reports/summary`) brings the day
  totals of every visible mower; the site filter uses Fleet's `site_id`. CSV export and "copy as
  table" are built in the browser (`$lib/reports/csv.ts`).
-->
<script lang="ts">
  import {
    Button,
    Field,
    Icon,
    Panel,
    ProblemNotice,
    Readout,
    SegmentedControl,
    Select,
    TopBar,
  } from '@mowdeck/ui';

  import { telemetryApi, unwrap } from '$lib/api/client';
  import { asProblem, type ApiProblem } from '$lib/api/problem';
  import DailyBars from '$lib/components/DailyBars.svelte';
  import SlowLoading from '$lib/components/SlowLoading.svelte';
  import { loadPlaces, type Places } from '$lib/fleet/data';
  import { sortSites } from '$lib/fleet/places';
  import { durationParts } from '$lib/health/series';
  import { formatNumber, problemMessage } from '$lib/i18n';
  import { m } from '$lib/paraglide/messages';
  import { getLocale } from '$lib/paraglide/runtime';
  import {
    PERIODS,
    customRangeIssue,
    dailySeries,
    elapsedHours,
    faultsByCode,
    periodRange,
    reportRows,
    reportTable,
    reportTotals,
    type DayRange,
    type Period,
    type ReportDay,
  } from '$lib/reports/aggregate';
  import { CSV_DIALECTS, csvFileName, toCsv, toTsv } from '$lib/reports/csv';
  import { toasts } from '$lib/stores/ui.svelte';

  let places = $state<Places | null>(null);
  let problem = $state<ApiProblem | null>(null);
  let siteId = $state('');
  let period = $state<Period>('7d');
  let custom = $state<DayRange>(periodRange('7d', new Date()));
  let dialect = $state<'de' | 'en'>(getLocale() === 'de' ? 'de' : 'en');
  let days = $state.raw(new Map<string, ReportDay[]>());
  let loadedRange = $state<DayRange | null>(null);
  let loading = $state(false);
  let truncated = $state(false);
  let now = $state(new Date());

  $effect(() => {
    void (async () => {
      try {
        places = await loadPlaces();
      } catch (error) {
        problem = asProblem(error);
      }
    })();
  });

  const customIssue = $derived(period === 'custom' ? customRangeIssue(custom, now) : null);
  const range = $derived(periodRange(period, now, custom));

  async function loadSummary(wanted: DayRange): Promise<void> {
    loading = true;
    try {
      const summary = await unwrap(
        telemetryApi.GET('/api/v1/telemetry/reports/summary', {
          params: { query: { from: wanted.from, to: wanted.to } },
        }),
      );
      days = new Map(summary.items.map((item) => [String(item.mower_id), item.days]));
      truncated = summary.truncated;
      loadedRange = wanted;
      problem = null;
    } catch (error) {
      problem = asProblem(error);
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    if (customIssue) return;
    const wanted = range;
    void loadSummary({ from: wanted.from, to: wanted.to });
  });

  function refresh(): void {
    now = new Date();
    if (!customIssue) void loadSummary(periodRange(period, now, custom));
  }

  const sites = $derived(sortSites(places?.sites ?? []));
  const siteName = $derived(new Map(sites.map((s) => [s.id, s.name])));
  const mowers = $derived((places?.mowers ?? []).filter((x) => !siteId || x.site_id === siteId));
  const shownRange = $derived(loadedRange ?? range);
  const hours = $derived(elapsedHours(shownRange, now));
  const rows = $derived(reportRows(mowers, days, hours));
  const totals = $derived(reportTotals(rows));
  const faults = $derived(faultsByCode(rows));
  const series = $derived(
    dailySeries(
      shownRange,
      days,
      mowers.map((x) => x.id),
      (d) => d.mowing_hours,
    ),
  );
  const scopeName = $derived(siteId ? (siteName.get(siteId) ?? '') : m.reports_all_sites());
  const locale = $derived(getLocale());

  /** The unit of hours (not translated, like every unit). */
  const HOURS = 'h';

  const PERIOD_LABELS: Record<Period, () => string> = {
    today: () => m.reports_period_today(),
    '7d': () => m.reports_period_7d(),
    '30d': () => m.reports_period_30d(),
    custom: () => m.reports_period_custom(),
  };

  function hoursText(value: number): string {
    return formatNumber(value, value < 10 ? 2 : 1);
  }

  /** A total duration: minutes below one hour, hours above (as on the health page). */
  function duration(value: number): { text: string; unit: string } {
    const parts = durationParts(value);
    return { text: formatNumber(parts.value, parts.digits), unit: parts.unit };
  }

  function distance(metres: number): { text: string; unit: string } {
    return metres >= 1000
      ? { text: formatNumber(metres / 1000, 2), unit: 'km' }
      : { text: formatNumber(metres), unit: 'm' };
  }

  /** A share as percent: one decimal below 10 %, so a short online time does not read 0. */
  function percent(share: number): string {
    const value = share * 100;
    return formatNumber(value, value > 0 && value < 10 ? 1 : 0);
  }

  function shortDay(day: string): string {
    return new Intl.DateTimeFormat(locale, {
      day: '2-digit',
      month: '2-digit',
      timeZone: 'UTC',
    }).format(new Date(`${day}T00:00:00Z`));
  }

  function table() {
    return reportTable(
      rows,
      totals,
      shownRange,
      (id) => siteName.get(id) ?? '',
      m.reports_total(),
      CSV_DIALECTS[dialect],
    );
  }

  function download(): void {
    const csv = toCsv(table(), CSV_DIALECTS[dialect]);
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = csvFileName(scopeName, shownRange.from, shownRange.to);
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1_000);
  }

  async function copy(): Promise<void> {
    const text = toTsv(table(), CSV_DIALECTS[dialect]);
    try {
      await navigator.clipboard.writeText(text);
      toasts.push('ok', m.reports_copied());
    } catch {
      toasts.push('error', m.reports_copy_failed());
    }
  }

  function issueText(issue: NonNullable<typeof customIssue>): string {
    switch (issue) {
      case 'missing':
        return m.reports_err_missing();
      case 'order':
        return m.reports_err_order();
      case 'too_long':
        return m.reports_err_too_long();
      case 'future':
        return m.reports_err_future();
    }
  }

  function errorLabel(code: string): string {
    return code === 'unknown' ? m.health_errors_unknown() : code;
  }
</script>

<svelte:head><title>{m.reports_title()} · {m.app_name()}</title></svelte:head>

<TopBar title={m.reports_title()} meta={`${shownRange.from} – ${shownRange.to}`}>
  {#snippet actions()}
    <Button
      variant="ghost"
      icon="clock"
      label={m.reports_refresh()}
      pending={loading}
      testid="report-refresh"
      onclick={refresh}
    />
  {/snippet}
</TopBar>

<div class="controls">
  <Select
    id="report-site"
    label={m.reports_scope()}
    options={[
      { value: '', label: m.reports_all_sites() },
      ...sites.map((s) => ({ value: s.id, label: s.name })),
    ]}
    bind:value={siteId}
  />
  <SegmentedControl
    label={m.reports_period()}
    options={PERIODS.map((p) => ({ value: p, label: PERIOD_LABELS[p]() }))}
    value={period}
    onchange={(value) => (period = value as Period)}
    testid="report-period"
  />
  {#if period === 'custom'}
    <div class="custom">
      <Field id="report-from" type="date" label={m.reports_from()} bind:value={custom.from} />
      <Field
        id="report-to"
        type="date"
        label={m.reports_to()}
        bind:value={custom.to}
        error={customIssue ? issueText(customIssue) : null}
      />
    </div>
  {/if}
</div>

{#if problem}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if !places || !loadedRange}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{:else}
  <section class="totals" aria-label={m.reports_totals()} data-testid="report-totals">
    <Readout
      label={m.reports_mowing()}
      value={duration(totals.mowingHours).text}
      unit={duration(totals.mowingHours).unit}
      size="primary"
      emptyText={m.no_data()}
      testid="report-total-mowing"
    />
    <Readout
      label={m.reports_blade()}
      value={duration(totals.bladeHours).text}
      unit={duration(totals.bladeHours).unit}
      emptyText={m.no_data()}
      testid="report-total-blade"
    />
    <Readout
      label={m.reports_distance()}
      value={distance(totals.distanceM).text}
      unit={distance(totals.distanceM).unit}
      emptyText={m.no_data()}
      testid="report-total-distance"
    />
    <Readout
      label={m.reports_jobs()}
      value={formatNumber(totals.jobs)}
      emptyText={m.no_data()}
      testid="report-total-jobs"
    />
    <Readout
      label={m.reports_faults()}
      value={formatNumber(totals.faults)}
      tone={totals.faults > 0 ? 'error' : null}
      emptyText={m.no_data()}
      testid="report-total-faults"
    />
    <Readout
      label={m.reports_availability()}
      value={percent(totals.availability)}
      unit="%"
      emptyText={m.no_data()}
      testid="report-total-availability"
    />
  </section>
  {#if truncated}
    <p class="pad note">{m.reports_truncated()}</p>
  {/if}

  <div class="grid">
    <Panel label={m.reports_per_mower()} flush testid="report-rows">
      {#if rows.length}
        <div class="scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">{m.col_mower()}</th>
                <th scope="col" class="num">{m.reports_mowing()}</th>
                <th scope="col" class="num">{m.reports_blade()}</th>
                <th scope="col" class="num">{m.reports_distance()}</th>
                <th scope="col" class="num">{m.reports_jobs()}</th>
                <th scope="col" class="num">{m.reports_faults()}</th>
                <th scope="col" class="num">{m.reports_availability()}</th>
              </tr>
            </thead>
            <tbody>
              {#each rows as row (row.mowerId)}
                {@const dist = distance(row.distanceM)}
                <tr data-testid="report-row-{row.mowerId}">
                  <th scope="row">
                    <a href="/mowers/{row.mowerId}/health">{row.name}</a>
                    <span class="site">{siteName.get(row.siteId) ?? ''}</span>
                  </th>
                  <td class="num">{hoursText(row.mowingHours)}<small>{HOURS}</small></td>
                  <td class="num">{hoursText(row.bladeHours)}<small>{HOURS}</small></td>
                  <td class="num">{dist.text}<small>{dist.unit}</small></td>
                  <td class="num">{formatNumber(row.jobs)}</td>
                  <td class="num" class:bad={row.faults > 0}>{formatNumber(row.faults)}</td>
                  <td class="num">
                    <span class="avail">
                      <span class="meter" aria-hidden="true"
                        ><span class="fill" style:inline-size="{row.availability * 100}%"
                        ></span></span
                      >{percent(row.availability)}<small>%</small>
                    </span>
                  </td>
                </tr>
              {/each}
            </tbody>
            <tfoot>
              <tr data-testid="report-row-total">
                <th scope="row">{m.reports_total()}</th>
                <td class="num">{hoursText(totals.mowingHours)}<small>{HOURS}</small></td>
                <td class="num">{hoursText(totals.bladeHours)}<small>{HOURS}</small></td>
                <td class="num"
                  >{distance(totals.distanceM).text}<small>{distance(totals.distanceM).unit}</small
                  ></td
                >
                <td class="num">{formatNumber(totals.jobs)}</td>
                <td class="num">{formatNumber(totals.faults)}</td>
                <td class="num">{percent(totals.availability)}<small>%</small></td>
              </tr>
            </tfoot>
          </table>
        </div>
      {:else}
        <p class="pad quiet">{m.reports_no_mowers()}</p>
      {/if}
    </Panel>

    <div class="aside">
      <Panel label={m.reports_daily()} testid="report-daily">
        {#if series.values.some((v) => v > 0)}
          <DailyBars
            label={m.reports_daily_mowing()}
            values={series.values}
            days={series.days}
            format={(v) => formatNumber(v, 2)}
            unit="h"
            peakLabel={(value) => m.health_daily_peak({ value })}
            summary={m.health_daily_label({
              metric: m.reports_daily_mowing(),
              days: series.days.length,
              total: `${hoursText(totals.mowingHours)} h`,
            })}
            firstLabel={shortDay(series.days[0] ?? '')}
            lastLabel={shortDay(series.days.at(-1) ?? '')}
            testid="report-daily-mowing"
          />
        {:else}
          <p class="quiet">{m.reports_daily_empty()}</p>
        {/if}
      </Panel>

      <Panel label={m.reports_faults_by_code()} testid="report-faults">
        {#if faults.length}
          <ul class="codes">
            {#each faults as fault (fault.code)}
              <li>
                <span class="code"><Icon name="alert" size={16} />{errorLabel(fault.code)}</span>
                <span class="mono">{formatNumber(fault.count)}×</span>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="quiet">{m.reports_faults_none()}</p>
        {/if}
      </Panel>

      <Panel label={m.reports_export()} testid="report-export">
        <p class="quiet small">{m.reports_export_hint()}</p>
        <SegmentedControl
          label={m.reports_dialect()}
          options={[
            { value: 'de', label: m.reports_dialect_de() },
            { value: 'en', label: m.reports_dialect_en() },
          ]}
          value={dialect}
          onchange={(value) => (dialect = value === 'en' ? 'en' : 'de')}
          testid="report-dialect"
        />
        <div class="export">
          <Button variant="primary" icon="download" testid="report-csv" onclick={download}
            >{m.reports_csv()}</Button
          >
          <Button icon="table" testid="report-copy" onclick={copy}>{m.reports_copy()}</Button>
        </div>
      </Panel>
    </div>
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

  .note {
    border-inline-start: 3px solid var(--md-state-paused);
    background: var(--md-color-surface-sunken);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .controls {
    display: grid;
    gap: var(--md-space-3);
    padding: var(--md-space-3) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  .custom {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--md-space-3);
  }

  .totals {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--md-space-0_5);
    padding: var(--md-space-3) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  .totals > :global(:first-child) {
    grid-column: 1 / -1;
  }

  .grid {
    display: grid;
  }

  .aside {
    display: grid;
    align-content: start;
  }

  .scroll {
    overflow-x: auto;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--md-font-size-14);
  }

  th,
  td {
    padding: var(--md-space-2) var(--md-space-3);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    text-align: start;
    white-space: nowrap;
  }

  thead th {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  tbody th {
    font-weight: var(--md-weight-semibold);
  }

  tbody th a {
    display: inline-flex;
    align-items: center;
    min-height: 40px;
    color: var(--md-color-ink);
  }

  .site {
    display: block;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-regular);
  }

  tfoot th,
  tfoot td {
    border-top: var(--md-border-control) solid var(--md-color-ink);
    font-weight: var(--md-weight-semibold);
  }

  .num {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
    text-align: end;
  }

  thead .num {
    font-family: var(--md-font-sans);
  }

  .num small {
    margin-inline-start: var(--md-space-0_5);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
  }

  .bad {
    color: var(--md-state-error);
  }

  .avail {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-2);
  }

  .meter {
    position: relative;
    width: 48px;
    height: 6px;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface-sunken);
  }

  .fill {
    position: absolute;
    inset-block: 0;
    inset-inline-start: 0;
    background: var(--md-state-mowing);
  }

  .codes {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .codes li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    min-height: 40px;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    font-size: var(--md-font-size-14);
  }

  .code {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    color: var(--md-state-error);
    font-family: var(--md-font-mono);
  }

  .export {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
    margin-top: var(--md-space-3);
  }

  @media (min-width: 768px) {
    .controls {
      grid-template-columns: minmax(200px, 280px) auto;
      align-items: end;
    }

    .custom {
      grid-column: 1 / -1;
      max-width: 480px;
    }

    .totals {
      grid-template-columns: repeat(6, minmax(0, 1fr));
    }

    .totals > :global(:first-child) {
      grid-column: auto;
    }
  }

  @media (min-width: 1024px) {
    .grid {
      grid-template-columns: minmax(0, 2fr) minmax(300px, 1fr);
      align-items: start;
      gap: var(--md-space-4);
      padding: var(--md-space-4) var(--md-gutter);
    }
  }
</style>

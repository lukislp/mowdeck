<!--
  Theft tracking of one mower (Telemetry): the recorded track on a map, the state of the session
  (started by what, ends automatically when, last position and its age, silent mower), and the
  actions: share a link (the address with its token is shown once), the PDF report, end the
  tracking (false alarm or by hand), start it by hand (also the safe way to test the whole chain)
  and delete an ended session. The status is polled every 8 s while a session is active, asking
  only for the points after the newest one. Starting, stopping, links and deleting need
  `can_control` (checked first, and hidden for good after a 403); viewing and the report do not.
-->
<script lang="ts">
  import {
    Button,
    Icon,
    Panel,
    ProblemNotice,
    SegmentedControl,
    Sheet,
    TopBar,
    TrackMap,
  } from '@mowdeck/ui';
  import { untrack } from 'svelte';

  import { page } from '$app/state';

  import { fleetApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { clock } from '#lib/clock.svelte.js';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import { loadConfig, type MapTilesConfig } from '#lib/config.js';
  import type { components as Fleet } from '#lib/generated/fleet.js';
  import { formatAge, formatDateTime, formatNumber, problemMessage } from '#lib/i18n.js';
  import { permissions, revoked } from '#lib/live.svelte.js';
  import { m } from '#lib/paraglide/messages.js';
  import { getLocale } from '#lib/paraglide/runtime.js';
  import { toasts } from '#lib/stores/ui.svelte.js';
  import {
    createLink,
    deleteSession,
    fetchReport,
    getTracking,
    listLinks,
    revokeLink,
    saveBlob,
    startTracking,
    stopTracking,
    type StopReason,
    type TrackingLink,
    type TrackingSession,
    type TrackingStatus,
  } from '#lib/tracking/api.js';
  import { startPoller } from '#lib/tracking/poll.js';
  import { canShare, shareUrl } from '#lib/tracking/share.js';
  import { mergeTrack, newestAt, serverOffsetMs, type TrackPoint } from '#lib/tracking/track.js';

  type Mower = Fleet['schemas']['MowerOut'];
  type Sheets = 'stop' | 'share' | 'delete' | null;

  const id = $derived(page.params.id ?? '');

  let mower = $state.raw<Mower | null>(null);
  let notFound = $state(false);
  let problem = $state<ApiProblem | null>(null);
  let loaded = $state(false);
  let session = $state.raw<TrackingSession | null>(null);
  let track = $state.raw<TrackPoint[]>([]);
  let links = $state.raw<TrackingLink[]>([]);
  let photos = $state(0);
  let truncated = $state(false);
  let offset = $state(0);
  let canControl = $state(false);
  let tiles = $state.raw<MapTilesConfig | null>(null);
  let mapFailed = $state(false);
  let follow = $state(false);
  let sheet = $state<Sheets>(null);
  let busy = $state<'start' | 'stop' | 'report' | 'delete' | 'link' | null>(null);
  let actionProblem = $state<ApiProblem | null>(null);
  let hours = $state('24');
  /** The address of a just created link: shown once, cleared when the sheet closes. */
  let created = $state<string | null>(null);

  const locale = $derived(getLocale());
  const active = $derived(session?.active === true);
  const last = $derived(session?.last ?? (track.length ? track[track.length - 1]! : null));
  const activeLinks = $derived(links.filter((link) => link.active));
  const now = $derived(clock.now + offset);

  $effect(() => clock.hold(1000));
  $effect(() => {
    void loadConfig().then((config) => (tiles = config.mapTiles));
  });

  function apply(status: TrackingStatus, merge: boolean): void {
    session = status.session;
    track = merge ? mergeTrack(track, status.track ?? []) : mergeTrack([], status.track ?? []);
    links = status.links ?? [];
    photos = status.photos;
    truncated = status.truncated;
    offset = serverOffsetMs(status.server_time, Date.now());
  }

  async function load(mowerId: string): Promise<void> {
    loaded = false;
    notFound = false;
    problem = null;
    mower = null;
    session = null;
    track = [];
    links = [];
    follow = false;
    canControl = false;
    try {
      mower = await unwrap(
        fleetApi.GET('/api/v1/fleet/mowers/{mower_id}', {
          params: { path: { mower_id: mowerId } },
        }),
      );
      apply(await getTracking(mowerId), false);
    } catch (error) {
      const p = asProblem(error);
      if (p.status === 404) notFound = true;
      else problem = p;
      return;
    } finally {
      loaded = true;
    }
    void permissions
      .check(mowerId, ['can_control'])
      .then((result) => (canControl = result.can_control))
      .catch(() => (canControl = false));
  }

  $effect(() => {
    void load(id);
  });

  // Poll while a session is active (or none exists yet, so one that starts meanwhile appears).
  $effect(() => {
    const mowerId = id;
    if (!loaded || problem || notFound) return;
    if (session && !session.active) return;
    return startPoller(async () => {
      try {
        // Without a session id the server answers with the active session, else the newest.
        const status = await getTracking(mowerId, { after: untrack(() => newestAt(track)) });
        if (mowerId !== untrack(() => id)) return { stop: true };
        const same = status.session?.id === untrack(() => session?.id);
        if (same) {
          apply(status, true);
        } else {
          // Another session: the polled points are only the tail, so read it completely.
          apply(await getTracking(mowerId), false);
        }
        return status.session && !status.session.active ? { stop: true } : {};
      } catch (error) {
        const p = asProblem(error);
        return { failed: true, retryAfter: p.retryAfter };
      }
    });
  });

  async function run<T>(
    kind: NonNullable<typeof busy>,
    action: () => Promise<T>,
  ): Promise<T | null> {
    busy = kind;
    actionProblem = null;
    try {
      return await action();
    } catch (error) {
      const p = asProblem(error);
      if (p.status === 403) canControl = false;
      actionProblem = p;
      return null;
    } finally {
      busy = null;
    }
  }

  async function start(): Promise<void> {
    const started = await run('start', () => startTracking(id));
    if (!started) return;
    toasts.push('ok', m.tracking_started_toast());
    await load(id);
  }

  async function stop(reason: StopReason): Promise<void> {
    const ended = await run('stop', () => stopTracking(id, reason));
    if (!ended) return;
    sheet = null;
    session = ended;
    toasts.push('ok', m.tracking_stopped_toast());
    void getTracking(id, { sessionId: ended.id })
      .then((status) => apply(status, false))
      .catch(() => undefined);
  }

  async function remove(): Promise<void> {
    const current = session;
    if (!current) return;
    const done = await run('delete', () => deleteSession(id, current.id).then(() => true));
    if (!done) return;
    sheet = null;
    toasts.push('ok', m.tracking_deleted());
    await load(id);
  }

  async function report(): Promise<void> {
    const current = session;
    if (!current) return;
    const blob = await run('report', () =>
      fetchReport(id, current.id, locale === 'de' ? 'de' : 'en'),
    );
    if (!blob) return;
    saveBlob(blob, `mowdeck-tracking-${current.started_at.slice(0, 10)}.pdf`);
    toasts.push('ok', m.tracking_report_ready());
  }

  async function makeLink(): Promise<void> {
    const result = await run('link', () => createLink(id, Number(hours)));
    if (!result) return;
    created = shareUrl(location.origin, result.token);
    void listLinks(id)
      .then((list) => (links = list))
      .catch(() => (links = [...links, result.link]));
  }

  function closeSheet(): void {
    sheet = null;
    created = null;
    actionProblem = null;
  }

  async function copyLink(): Promise<void> {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created);
      toasts.push('ok', m.tracking_share_copied());
    } catch {
      /* The field is selected for a manual copy. */
    }
  }

  async function shareLink(): Promise<void> {
    if (!created) return;
    try {
      await navigator.share({
        title: m.tracking_share_message({ mower: mower?.name ?? '' }),
        url: created,
      });
    } catch {
      /* Cancelled by the user. */
    }
  }

  async function revoke(link: TrackingLink): Promise<void> {
    const done = await run('link', () => revokeLink(id, link.id).then(() => true));
    if (!done) return;
    links = links.filter((entry) => entry.id !== link.id);
    toasts.push('ok', m.tracking_link_revoked());
  }

  // --- view model -------------------------------------------------------------------------------

  function triggerLabel(trigger: TrackingSession['trigger']): string {
    switch (trigger) {
      case 'mower_lifted':
        return m.tracking_trigger_mower_lifted();
      case 'mower_left_dock_unexpectedly':
        return m.tracking_trigger_mower_left_dock_unexpectedly();
      case 'geofence_exit':
        return m.tracking_trigger_geofence_exit();
      case 'manual':
        return m.tracking_trigger_manual();
    }
  }

  function endLabel(reason: TrackingSession['end_reason']): string {
    switch (reason) {
      case 'false_alarm':
        return m.tracking_end_false_alarm();
      case 'max_duration':
        return m.tracking_end_max_duration();
      default:
        return m.tracking_end_stopped();
    }
  }

  const shortTime = (value: string) =>
    new Intl.DateTimeFormat(locale, { timeStyle: 'short' }).format(new Date(value));

  const mapLabels = $derived({
    region: m.tracking_map_region(),
    zoomIn: m.tracking_map_zoom_in(),
    zoomOut: m.tracking_map_zoom_out(),
    fit: m.tracking_map_fit(),
    follow: m.tracking_map_follow(),
    start: m.tracking_map_start(),
    last: m.tracking_map_last(),
    noTiles: m.tracking_map_no_tiles(),
  });

  const hourOptions = $derived(
    ['1', '6', '24', '48'].map((value) => ({
      value,
      label: m.tracking_share_hours_option({ hours: value }),
    })),
  );

  const coordinates = (point: { lat: number; lon: number }) =>
    `${formatNumber(point.lat, 5)}, ${formatNumber(point.lon, 5)}`;
</script>

<svelte:head><title>{m.tracking_title()} · {mower?.name ?? m.app_name()}</title></svelte:head>

{#if notFound || revoked.has(id)}
  <TopBar title={m.detail_not_found_title()} backHref="/mowers" backLabel={m.back()} />
  <div class="pad"><ProblemNotice tone="info" title={m.detail_not_found_title()} /></div>
{:else if problem}
  <TopBar title={m.tracking_title()} backHref="/mowers/{id}" backLabel={m.back()} />
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if !loaded}
  <TopBar title={m.tracking_title()} backHref="/mowers/{id}" backLabel={m.back()} />
  <p class="pad" role="status"><SlowLoading /></p>
{:else}
  <TopBar
    title={m.tracking_title()}
    meta={mower?.name ?? ''}
    backHref="/mowers/{id}"
    backLabel={m.back()}
  />

  <div class="view" data-testid="tracking-page" data-active={active}>
    {#if session}
      <div class="map">
        {#if mapFailed}
          <div class="pad"><ProblemNotice title={m.tracking_map_failed()} /></div>
        {:else}
          <TrackMap
            {track}
            {last}
            stale={Boolean(session.stale_since)}
            {tiles}
            labels={mapLabels}
            bind:follow
            onfail={() => (mapFailed = true)}
          />
        {/if}
      </div>
    {/if}

    <div class="side">
      {#if session}
        <section class="status" data-testid="tracking-status">
          <div class="state" role="status" aria-live="polite">
            {#if active}
              <span class="chip live"
                ><span class="dot" aria-hidden="true"></span>{m.tracking_status_active()}</span
              >
            {:else}
              <span class="chip"><Icon name="check" size={16} />{m.tracking_status_ended()}</span>
            {/if}
            <span class="trigger">{triggerLabel(session.trigger)}</span>
          </div>

          <dl class="facts">
            <div>
              <dt>{m.tracking_label_last()}</dt>
              <dd>
                {#if last}
                  <strong class="age" data-testid="tracking-age"
                    >{m.tracking_last_seen({ age: formatAge(last.at, now) })}</strong
                  >
                  <span class="mono">{coordinates(last)}</span>
                {:else}
                  {m.tracking_no_position()}
                {/if}
              </dd>
            </div>
            <div>
              <dt>{m.tracking_label_started()}</dt>
              <dd>{formatDateTime(session.started_at)}</dd>
            </div>
            {#if active}
              <div>
                <dt>{m.tracking_label_ends()}</dt>
                <dd>{shortTime(session.ends_at)}</dd>
              </div>
            {:else if session.ended_at}
              <div>
                <dt>{m.tracking_label_ended()}</dt>
                <dd>{formatDateTime(session.ended_at)} · {endLabel(session.end_reason)}</dd>
              </div>
            {/if}
          </dl>

          {#if active && session.stale_since}
            <p class="stale" role="status" data-testid="tracking-stale">
              <Icon name="offline" size={18} />
              {m.tracking_stale({ time: shortTime(session.stale_since) })}
            </p>
          {/if}

          <p class="counts mono">
            {m.tracking_points({ count: formatNumber(session.points) })} · {m.tracking_photos({
              count: formatNumber(photos),
            })}
          </p>
          {#if truncated}<p class="hint">{m.tracking_truncated()}</p>{/if}
        </section>

        <div class="actions">
          {#if canControl}
            <Button icon="send" full testid="tracking-share" onclick={() => (sheet = 'share')}
              >{m.tracking_share()}</Button
            >
          {/if}
          <Button
            icon="download"
            full
            pending={busy === 'report'}
            testid="tracking-report"
            onclick={report}>{m.tracking_report()}</Button
          >
          {#if canControl && active}
            <Button
              variant="danger"
              icon="stop"
              full
              testid="tracking-stop"
              onclick={() => (sheet = 'stop')}>{m.tracking_stop()}</Button
            >
          {:else if canControl}
            <Button
              variant="ghost"
              icon="trash"
              full
              testid="tracking-delete"
              onclick={() => (sheet = 'delete')}>{m.tracking_delete()}</Button
            >
          {/if}
        </div>
      {:else}
        <section class="idle" data-testid="tracking-idle">
          <h2>{m.tracking_idle_title()}</h2>
          <p>{m.tracking_idle_text()}</p>
          {#if canControl}
            <p class="hint">{m.tracking_start_hint()}</p>
            <Button
              variant="primary"
              icon="play"
              full
              pending={busy === 'start'}
              testid="tracking-start"
              onclick={start}>{m.tracking_start()}</Button
            >
          {/if}
        </section>
      {/if}

      {#if actionProblem && sheet === null}
        <ProblemNotice
          title={problemMessage(actionProblem)}
          reference={actionProblem.correlationId}
          referenceLabel={m.problem_reference()}
        />
      {/if}

      {#if session}
        <Panel label={m.tracking_links_title()} testid="tracking-links">
          {#if activeLinks.length === 0}
            <p class="hint">{m.tracking_links_empty()}</p>
          {:else}
            <ul class="links">
              {#each activeLinks as link (link.id)}
                <li>
                  <div>
                    <span>{m.tracking_link_created({ time: formatDateTime(link.created_at) })}</span
                    >
                    <span class="mono"
                      >{m.tracking_link_expires({ time: formatDateTime(link.expires_at) })} · {m.tracking_link_uses(
                        { count: formatNumber(link.uses) },
                      )}{link.last_used_at
                        ? ` · ${m.tracking_link_last_used({ age: formatAge(link.last_used_at, now) })}`
                        : ''}</span
                    >
                  </div>
                  {#if canControl}
                    <Button
                      variant="ghost"
                      icon="close"
                      pending={busy === 'link'}
                      onclick={() => revoke(link)}>{m.tracking_link_revoke()}</Button
                    >
                  {/if}
                </li>
              {/each}
            </ul>
          {/if}
        </Panel>
      {/if}
    </div>
  </div>

  <Sheet
    open={sheet === 'stop'}
    title={m.tracking_stop_title()}
    closeLabel={m.close()}
    onclose={closeSheet}
    testid="tracking-stop-sheet"
  >
    <p>{m.tracking_stop_text()}</p>
    <div class="choices">
      <button
        type="button"
        class="choice"
        disabled={busy === 'stop'}
        data-testid="tracking-stop-false-alarm"
        onclick={() => stop('false_alarm')}
      >
        <strong>{m.tracking_stop_false_alarm()}</strong>
        <span>{m.tracking_stop_false_alarm_hint()}</span>
      </button>
      <button
        type="button"
        class="choice"
        disabled={busy === 'stop'}
        data-testid="tracking-stop-stopped"
        onclick={() => stop('stopped')}
      >
        <strong>{m.tracking_stop_stopped()}</strong>
        <span>{m.tracking_stop_stopped_hint()}</span>
      </button>
    </div>
    {#if actionProblem}<ProblemNotice title={problemMessage(actionProblem)} />{/if}
  </Sheet>

  <Sheet
    open={sheet === 'delete'}
    title={m.tracking_delete()}
    closeLabel={m.close()}
    onclose={closeSheet}
    testid="tracking-delete-sheet"
  >
    <p>{m.tracking_delete_confirm()}</p>
    {#if actionProblem}<ProblemNotice title={problemMessage(actionProblem)} />{/if}
    {#snippet footer()}
      <Button onclick={closeSheet}>{m.cancel()}</Button>
      <Button variant="danger" icon="trash" pending={busy === 'delete'} onclick={remove}
        >{m.tracking_delete()}</Button
      >
    {/snippet}
  </Sheet>

  <Sheet
    open={sheet === 'share'}
    title={m.tracking_share_title()}
    closeLabel={m.close()}
    onclose={closeSheet}
    testid="tracking-share-sheet"
  >
    {#if created}
      <p role="status">{m.tracking_share_once()}</p>
      <label class="address">
        <span>{m.tracking_share_address()}</span>
        <input
          type="text"
          readonly
          value={created}
          data-testid="tracking-share-url"
          onfocus={(event) => event.currentTarget.select()}
        />
      </label>
      <div class="row">
        <Button icon="copy" onclick={copyLink} testid="tracking-share-copy"
          >{m.tracking_share_copy()}</Button
        >
        {#if canShare()}
          <Button icon="send" variant="primary" onclick={shareLink}
            >{m.tracking_share_send()}</Button
          >
        {/if}
      </div>
    {:else}
      <p>{m.tracking_share_text()}</p>
      <SegmentedControl
        label={m.tracking_share_hours()}
        options={hourOptions}
        value={hours}
        onchange={(value) => (hours = value)}
      />
      {#if actionProblem}<ProblemNotice title={problemMessage(actionProblem)} />{/if}
      <Button
        variant="primary"
        full
        pending={busy === 'link'}
        testid="tracking-share-create"
        onclick={makeLink}>{m.tracking_share_create()}</Button
      >
    {/if}
  </Sheet>
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
    color: var(--md-color-ink-muted);
  }

  .view {
    display: grid;
    gap: 0;
  }

  .map {
    block-size: 52dvh;
    min-block-size: 280px;
    border-block-end: var(--md-hairline) solid var(--md-color-line-strong);
  }

  .side {
    display: grid;
    gap: var(--md-space-4);
    align-content: start;
    padding: var(--md-space-4) var(--md-gutter) var(--md-space-12);
  }

  @media (min-width: 900px) {
    .view {
      grid-template-columns: minmax(0, 1fr) 400px;
      min-block-size: calc(100dvh - var(--md-sticky-top));
    }

    .map {
      block-size: auto;
      min-block-size: 0;
      border-block-end: 0;
      border-inline-end: var(--md-hairline) solid var(--md-color-line-strong);
    }

    .view:not([data-active]) .side {
      grid-column: 1 / -1;
    }
  }

  .state {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2) var(--md-space-3);
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-2);
    padding: var(--md-space-1) var(--md-space-3);
    border: var(--md-border-control) solid var(--md-state-docked);
    border-radius: var(--md-radius-round);
    background: var(--md-state-docked-bg);
    color: var(--md-state-docked);
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-semibold);
  }

  .chip.live {
    border-color: var(--md-state-error);
    background: var(--md-state-error-bg);
    color: var(--md-state-error);
  }

  .dot {
    inline-size: 10px;
    block-size: 10px;
    border-radius: var(--md-radius-round);
    background: var(--md-state-error);
    animation: blink 1.4s ease-in-out infinite;
  }

  @keyframes blink {
    50% {
      opacity: 0.25;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .dot {
      animation: none;
    }
  }

  .trigger {
    font-size: var(--md-font-size-14);
    color: var(--md-color-ink-muted);
  }

  .facts {
    display: grid;
    gap: var(--md-space-3);
    margin: var(--md-space-4) 0 0;
  }

  .facts div {
    display: grid;
    gap: var(--md-space-0_5);
  }

  dt {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  dd {
    display: grid;
    gap: var(--md-space-0_5);
    margin: 0;
    font-size: var(--md-font-size-16);
  }

  .age {
    font-size: var(--md-font-size-20);
    font-weight: var(--md-weight-semibold);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
  }

  .stale {
    display: flex;
    align-items: flex-start;
    gap: var(--md-space-2);
    margin: var(--md-space-4) 0 0;
    padding: var(--md-space-3);
    border-inline-start: 4px solid var(--md-state-paused);
    background: var(--md-state-paused-bg);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
  }

  .counts {
    margin: var(--md-space-4) 0 0;
    color: var(--md-color-ink-muted);
  }

  .hint {
    margin: var(--md-space-2) 0 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .actions {
    display: grid;
    gap: var(--md-space-3);
  }

  .idle h2 {
    margin: 0 0 var(--md-space-2);
    font-size: var(--md-font-size-20);
  }

  .idle p {
    margin: 0 0 var(--md-space-3);
  }

  .links {
    display: grid;
    gap: var(--md-space-3);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .links li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-3);
  }

  .links li div {
    display: grid;
    min-inline-size: 0;
    font-size: var(--md-font-size-14);
  }

  .choices {
    display: grid;
    gap: var(--md-space-3);
    margin: var(--md-space-3) 0;
  }

  .choice {
    display: grid;
    gap: var(--md-space-1);
    min-block-size: var(--md-target-command);
    padding: var(--md-space-3) var(--md-space-4);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-3);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font: inherit;
    text-align: start;
    cursor: pointer;
  }

  .choice span {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .choice:focus-visible {
    outline: 3px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .choice:disabled {
    opacity: 0.6;
    cursor: default;
  }

  .address {
    display: grid;
    gap: var(--md-space-1);
    margin: var(--md-space-3) 0;
    font-size: var(--md-font-size-13);
  }

  .address input {
    min-block-size: var(--md-target);
    padding: 0 var(--md-space-3);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface-sunken);
    color: var(--md-color-ink);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-3);
  }
</style>

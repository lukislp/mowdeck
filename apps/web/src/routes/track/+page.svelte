<!--
  The public page behind a share link (`/track#<token>`): no account, no navigation. It reads the
  token from the URL fragment, removes it from the address bar at once, and asks the public
  tracking endpoint every 8 s (plain fetch, no authentication). It shows the mower's name and
  model, the position and the track on a map, when the mower last reported, when tracking started
  or ended and until when the link is valid. An unusable link (unknown, revoked, expired) all look
  the same; a rate limit waits for Retry-After. The token stays in this tab's memory and, so a
  reload of the tab still works, in sessionStorage (cleared when the link turns out unusable).
-->
<script lang="ts">
  import { Icon, TrackMap } from '@mowdeck/ui';
  import { onMount, untrack } from 'svelte';

  import { clock } from '#lib/clock.svelte.js';
  import { loadConfig, type MapTilesConfig } from '#lib/config.js';
  import { formatAge, formatDateTime, formatMoment } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';
  import {
    loadLayer,
    mapLayers,
    publicTileLoader,
    publicTileUrl,
    saveLayer,
  } from '#lib/tracking/basemap.js';
  import { startPoller } from '#lib/tracking/poll.js';
  import PositionFix from '#lib/tracking/PositionFix.svelte';
  import { viewPublic, type PublicTrack } from '#lib/tracking/public.js';
  import { tokenFromHash } from '#lib/tracking/share.js';
  import { mergeTrack, serverOffsetMs } from '#lib/tracking/track.js';

  const STORE_KEY = 'mowdeck.track';

  type Phase = 'loading' | 'ok' | 'invalid';

  let phase = $state<Phase>('loading');
  let data = $state.raw<PublicTrack | null>(null);
  let offset = $state(0);
  let limited = $state<{ seconds: number | null } | null>(null);
  let offline = $state(false);
  let tiles = $state.raw<MapTilesConfig | null>(null);
  let mapFailed = $state(false);
  let follow = $state(false);
  let layer = $state<string | null>(loadLayer());
  /** The share token, kept in memory for the tile requests (never in a URL). */
  let token = $state<string | null>(null);

  const offeredKey = $derived(JSON.stringify(data?.map_layers ?? []));
  const baseLayers = $derived.by(() => {
    void offeredKey;
    const current = token;
    if (!current) return [];
    return mapLayers(
      untrack(() => data?.map_layers),
      publicTileUrl,
      publicTileLoader(current),
      (kind) => (kind === 'map' ? m.tracking_layer_map() : m.tracking_layer_aerial()),
    );
  });
  $effect(() => saveLayer(layer));

  const track = $derived(data ? mergeTrack([], data.track) : []);
  const last = $derived(data?.last ?? (track.length ? track[track.length - 1]! : null));
  const ended = $derived(data ? !data.active : false);
  const stale = $derived(Boolean(data?.active && data.stale_since));
  const now = $derived(clock.now + offset);

  function storage(): Storage | null {
    try {
      return window.sessionStorage;
    } catch {
      return null;
    }
  }

  function readToken(): string | null {
    const fromHash = tokenFromHash(location.hash);
    // The token leaves the address bar (history, shared screenshots, Referer) right away.
    if (location.hash) history.replaceState(null, '', location.pathname + location.search);
    if (fromHash) {
      try {
        storage()?.setItem(STORE_KEY, fromHash);
      } catch {
        /* Storage is optional. */
      }
      return fromHash;
    }
    try {
      return tokenFromHash(storage()?.getItem(STORE_KEY) ?? '');
    } catch {
      return null;
    }
  }

  function forget(): void {
    try {
      storage()?.removeItem(STORE_KEY);
    } catch {
      /* Storage is optional. */
    }
  }

  onMount(() => {
    const releaseClock = clock.hold(1000);
    void loadConfig().then((config) => (tiles = config.mapTiles));
    const read = readToken();
    token = read;
    let stopPolling = () => {};
    if (!read) {
      phase = 'invalid';
    } else {
      const abort = new AbortController();
      const stop = startPoller(async () => {
        const result = await viewPublic(read, undefined, abort.signal);
        if (abort.signal.aborted) return { stop: true };
        switch (result.kind) {
          case 'ok':
            data = result.data;
            offset = serverOffsetMs(result.data.server_time, Date.now());
            phase = 'ok';
            limited = null;
            offline = false;
            return result.data.active ? {} : { stop: true };
          case 'invalid':
            forget();
            phase = 'invalid';
            return { stop: true };
          case 'limited':
            limited = { seconds: result.retryAfter };
            return { failed: true, retryAfter: result.retryAfter };
          case 'error':
            offline = true;
            return { failed: true };
        }
      });
      stopPolling = () => {
        abort.abort();
        stop();
      };
    }
    return () => {
      stopPolling();
      releaseClock();
    };
  });

  const mapLabels = $derived({
    region: m.tracking_map_region(),
    zoomIn: m.tracking_map_zoom_in(),
    zoomOut: m.tracking_map_zoom_out(),
    fit: m.tracking_map_fit(),
    follow: m.tracking_map_follow(),
    start: m.tracking_map_start(),
    last: m.tracking_map_last(),
    noTiles: m.tracking_map_no_tiles(),
    layers: m.tracking_map_layers(),
    uncovered: (layer: string) => m.tracking_map_uncovered({ layer }),
    noneHere: m.tracking_map_none_here(),
  });

  // The time alone only for today: an outage before midnight shows its date too.
  const shortTime = (value: string) => formatMoment(value, now);
</script>

<svelte:head>
  <title>{m.trackpub_title()}</title>
  <meta name="robots" content="noindex,nofollow,noarchive" />
  <meta name="referrer" content="no-referrer" />
</svelte:head>

<main class="public" data-testid="track-public">
  {#if phase === 'invalid'}
    <section class="message" data-testid="track-invalid">
      <Icon name="alert" size={32} />
      <h1>{m.trackpub_invalid_title()}</h1>
      <p>{m.trackpub_invalid_text()}</p>
    </section>
  {:else if phase === 'loading'}
    <section class="message" role="status">
      <p>{m.trackpub_loading()}</p>
      {#if limited}<p>
          {limited.seconds
            ? m.trackpub_limited({ seconds: limited.seconds })
            : m.trackpub_limited_unknown()}
        </p>{/if}
      {#if offline}<p>{m.trackpub_offline()}</p>{/if}
    </section>
  {:else if data}
    <header class="head">
      <h1>{data.mower_name}</h1>
      {#if data.mower_model}<span class="model">{data.mower_model}</span>{/if}
      <span class="chip" class:live={!ended} role="status">
        {#if ended}
          {m.tracking_status_ended()}
        {:else}
          <span class="dot" aria-hidden="true"></span>{m.trackpub_live()}
        {/if}
      </span>
    </header>

    <div class="map">
      {#if !mapFailed}
        <TrackMap
          {track}
          {last}
          {stale}
          layers={baseLayers}
          bind:layer
          {tiles}
          labels={mapLabels}
          bind:follow
          onfail={() => (mapFailed = true)}
        />
      {:else}
        <p class="message">{m.tracking_map_failed()}</p>
      {/if}
    </div>

    <section class="info" aria-live="polite">
      <p class="seen" data-testid="track-age">
        {#if last}
          {m.trackpub_last_seen({ age: formatAge(last.at, now) })}
        {:else}
          {m.tracking_no_position()}
        {/if}
      </p>
      {#if last}
        <PositionFix point={last} {now} testid="track-fix" />
      {/if}
      {#if stale && data.stale_since}
        <p class="banner warn" data-testid="track-stale">
          <Icon name="offline" size={18} />
          {m.tracking_stale({ time: shortTime(data.stale_since) })}
        </p>
      {/if}
      {#if ended}
        <p class="banner" data-testid="track-ended">
          <Icon name="check" size={18} />
          {m.trackpub_ended_banner()}
        </p>
      {/if}
      {#if limited}
        <p class="banner warn" data-testid="track-limited">
          {limited.seconds
            ? m.trackpub_limited({ seconds: limited.seconds })
            : m.trackpub_limited_unknown()}
        </p>
      {:else if offline}
        <p class="banner warn">{m.trackpub_offline()}</p>
      {/if}
      <p class="meta mono">
        {m.trackpub_started({ time: formatDateTime(data.started_at) })}
        {#if data.ended_at}<br />{m.trackpub_ended({ time: formatDateTime(data.ended_at) })}{/if}
        <br />{m.trackpub_valid_until({ time: formatDateTime(data.expires_at) })}
      </p>
    </section>
  {/if}
</main>

<style>
  .public {
    display: grid;
    grid-template-rows: auto minmax(0, 1fr) auto;
    min-block-size: 100dvh;
    background: var(--md-color-bg);
    color: var(--md-color-ink);
  }

  .message {
    grid-row: 1 / -1;
    display: grid;
    align-content: center;
    justify-items: center;
    gap: var(--md-space-3);
    padding: var(--md-space-8) var(--md-gutter);
    text-align: center;
  }

  .message h1 {
    margin: 0;
    font-size: var(--md-font-size-24);
  }

  .message p {
    max-inline-size: 36ch;
    margin: 0;
    color: var(--md-color-ink-muted);
  }

  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--md-space-1) var(--md-space-3);
    padding: calc(var(--md-safe-top) + var(--md-space-3)) var(--md-gutter) var(--md-space-3);
    border-block-end: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  .head h1 {
    margin: 0;
    font-size: var(--md-font-size-20);
  }

  .model {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-2);
    margin-inline-start: auto;
    padding: var(--md-space-0_5) var(--md-space-3);
    border: var(--md-border-control) solid var(--md-state-docked);
    border-radius: var(--md-radius-round);
    background: var(--md-state-docked-bg);
    color: var(--md-state-docked);
    font-size: var(--md-font-size-13);
    font-weight: var(--md-weight-semibold);
  }

  .chip.live {
    border-color: var(--md-state-error);
    background: var(--md-state-error-bg);
    color: var(--md-state-error);
  }

  .dot {
    inline-size: 9px;
    block-size: 9px;
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

  .map {
    min-block-size: max(320px, 50dvh);
  }

  .info {
    display: grid;
    gap: var(--md-space-2);
    padding: var(--md-space-4) var(--md-gutter)
      calc(var(--md-space-4) + env(safe-area-inset-bottom, 0px));
    border-block-start: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface);
  }

  .info p {
    margin: 0;
  }

  .seen {
    font-size: var(--md-font-size-20);
    font-weight: var(--md-weight-semibold);
  }

  .banner {
    display: flex;
    align-items: flex-start;
    gap: var(--md-space-2);
    padding: var(--md-space-3);
    border-inline-start: 4px solid var(--md-state-docked);
    background: var(--md-state-docked-bg);
    font-size: var(--md-font-size-14);
  }

  .banner.warn {
    border-inline-start-color: var(--md-state-paused);
    background: var(--md-state-paused-bg);
  }

  .meta {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-20);
  }

  .mono {
    font-family: var(--md-font-mono);
  }
</style>

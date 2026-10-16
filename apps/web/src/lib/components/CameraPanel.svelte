<!--
  The live camera of one mower (ADR 0032) with its controls. The picture itself is
  `CameraStage` (shared with the drive view, ADR 0044): it owns the viewer and with it the one
  camera session of this panel, draws the tiles (test pattern for the simulator's `fake`
  sessions, the Agora video otherwise) and marks a standing picture. Unmounting the panel (the
  mower page's collapse toggle) or leaving the page leaves the channel. The picker lists only
  cameras that publish video; with two front cameras (a stereo pair) it also offers "Both". The
  data note reminds that the video goes through Mammotion's and Agora's servers and counts
  against the 4G allowance. Two collapsible lines follow for real cameras: "Diagnose" (receive
  statistics every 2 s) and "Advanced" (connection options stored per browser).
-->
<script lang="ts">
  import { Button, SegmentedControl, Switch } from '@mowdeck/ui';

  import { type DelaySource, shown } from '$lib/camera/diagnostics';
  import { BOTH, cameraOptions as optionsOf } from '$lib/camera/labels';
  import type { CameraViewer } from '$lib/camera/viewer.svelte';
  import { formatNumber, formatTime } from '$lib/i18n';
  import { m } from '$lib/paraglide/messages';

  import CameraStage from './CameraStage.svelte';

  interface Props {
    mowerId: string;
    mowerName: string;
    /** The mower's latest position (for the test pattern). */
    position?: { lat: number; lon: number } | null;
    /** The full-screen page: no full-screen link, a taller stage. */
    fullscreen?: boolean;
    /** Show the full-screen link in the panel's own head (off when the host shows it). */
    link?: boolean;
    onclose?: () => void;
  }

  const delayLabel = (source: DelaySource): string =>
    ({
      end2end: m.camera_diag_delay_end2end,
      receive: m.camera_diag_delay_receive,
      transport: m.camera_diag_delay_transport,
      rtt: m.camera_diag_delay_rtt,
    })[source]();

  let {
    mowerId,
    mowerName,
    position = null,
    fullscreen = false,
    link = true,
    onclose,
  }: Props = $props();

  /** The stage's viewer (set as soon as the stage is created). */
  let viewer = $state<CameraViewer | null>(null);

  const fake = $derived(viewer?.session?.provider === 'fake');
  const cameraOptions = $derived(
    viewer
      ? optionsOf(viewer.cameras, viewer.publishing, viewer.cameraUid, viewer.pair !== null)
      : [],
  );
  const pickerValue = $derived(
    viewer?.both ? BOTH : String(viewer?.cameraUid ?? viewer?.activeUid ?? ''),
  );

  function pick(value: string): void {
    if (value === BOTH) void viewer?.showBoth();
    else void viewer?.select(Number(value));
  }

  /** Test ids of the picker's segments (the layout by name, cameras by uid). */
  function pickTestid(value: string): string {
    return value === BOTH ? `camera-layout-${value}` : `camera-pick-${value}`;
  }

  const diag = $derived(viewer?.diagnostics ?? null);
  const stamp = (at: number): string => formatTime(new Date(at).toISOString());
  const quota = $derived(viewer?.session?.quota ?? null);
</script>

<div
  class="camera"
  data-testid="camera-panel"
  data-phase={viewer?.phase ?? 'idle'}
  data-provider={viewer?.session?.provider ?? ''}
>
  {#if (link && !fullscreen) || onclose}
    <div class="head">
      {#if link && !fullscreen}
        <Button
          variant="ghost"
          icon="expand"
          href="/mowers/{mowerId}/camera"
          label={m.camera_fullscreen()}
          testid="camera-fullscreen"
        />
      {/if}
      {#if onclose}
        <Button
          variant="ghost"
          icon="close"
          label={m.camera_close()}
          testid="camera-close"
          onclick={() => {
            void viewer?.close();
            onclose?.();
          }}
        />
      {/if}
    </div>
  {/if}

  <CameraStage {mowerId} {mowerName} {position} {fullscreen} bind:viewer />

  {#if viewer}
    {@const v = viewer}
    <div class="controls">
      {#if cameraOptions.length > 1}
        <div
          class="picker"
          role="radiogroup"
          aria-label={m.camera_picker()}
          data-testid="camera-picker"
        >
          {#each cameraOptions as option (option.value)}
            <button
              type="button"
              role="radio"
              aria-checked={option.value === pickerValue}
              data-value={option.value}
              data-testid={pickTestid(option.value)}
              onclick={() => pick(option.value)}>{option.label}</button
            >
          {/each}
        </div>
      {/if}
      {#if v.session && !fake}
        <Button
          variant="ghost"
          icon={v.muted ? 'mute' : 'sound'}
          label={v.muted ? m.camera_unmute() : m.camera_mute()}
          testid="camera-mute"
          onclick={() => v.toggleMute()}
        />
      {/if}
    </div>

    <p class="hint" data-testid="camera-data-note">{m.camera_data_note()}</p>
    {#if v.both && !fake}
      <p class="hint" data-testid="camera-both-note">{m.camera_both_note()}</p>
    {/if}
    {#if quota && quota.total_minutes > 0}
      <p class="hint mono" data-testid="camera-quota">
        {m.camera_quota({
          used: formatNumber(quota.used_minutes),
          total: formatNumber(quota.total_minutes),
        })}
      </p>
    {/if}
    {#if v.activeUid !== null}
      <p class="hint mono" data-testid="camera-active-uid">
        {m.camera_active_uid({ uid: String(v.activeUid) })}
      </p>
    {/if}
    {#if v.session}
      <p class="hint mono" data-testid="camera-expiry">
        {m.camera_expires({ time: formatTime(v.session.expires_at) })}
        {#if v.renewals}· {m.camera_renewed({ count: v.renewals })}{/if}
      </p>
    {/if}
    {#if v.session && !fake}
      <details
        class="fold"
        data-testid="camera-diagnose"
        ontoggle={(event) => v.watchDiagnostics((event.currentTarget as HTMLDetailsElement).open)}
      >
        <summary>{m.camera_diag_summary()}</summary>
        {#if diag}
          <div class="readout mono" data-testid="camera-diagnose-readout">
            <span>{m.camera_diag_state({ state: diag.state })}</span>
            <span>
              {m.camera_diag_video({
                bitrate: shown(diag.bitrateKbps),
                fps: shown(diag.fps),
                size: diag.width && diag.height ? `${diag.width}x${diag.height}` : '-',
              })}
            </span>
            <span>
              {m.camera_diag_quality({
                freeze: shown(diag.freezePercent),
                seconds: shown(diag.freezeSeconds),
              })}
            </span>
            <span>
              {m.camera_diag_network({
                loss: shown(diag.lossPercent),
                total: shown(diag.lossTotalPercent),
                delay: shown(diag.delayMs),
                source: diag.delaySource ? delayLabel(diag.delaySource) : '-',
                rtt: shown(diag.rttMs),
              })}
            </span>
            <span>
              {m.camera_diag_events({
                states: diag.stateChanges,
                published: diag.published,
                unpublished: diag.unpublished,
                rejoins: v.rejoins,
              })}
            </span>
            {#if diag.recent.length}
              <span>{m.camera_diag_last()}:</span>
              {#each diag.recent as event (event.at + event.text)}
                <span class="event">{stamp(event.at)} {event.kind} {event.text}</span>
              {/each}
            {/if}
          </div>
        {:else}
          <p class="hint mono">{m.camera_diag_none()}</p>
        {/if}
      </details>
      <details class="fold" data-testid="camera-advanced">
        <summary>{m.camera_advanced()}</summary>
        <div class="options">
          <p class="hint">{m.camera_opt_profile()}: {m.camera_opt_profile_hint()}</p>
          <SegmentedControl
            label={m.camera_opt_profile()}
            options={[
              { value: 'rtc', label: m.camera_opt_profile_rtc() },
              { value: 'live', label: m.camera_opt_profile_live() },
            ]}
            value={v.options.profile}
            onchange={(value) => void v.setOptions({ profile: value === 'live' ? 'live' : 'rtc' })}
            testid="camera-opt-profile"
          />
          {#if v.options.profile === 'live'}
            <p class="hint">{m.camera_opt_latency()}: {m.camera_opt_latency_hint()}</p>
            <SegmentedControl
              label={m.camera_opt_latency()}
              options={[
                { value: '1', label: m.camera_opt_latency_low() },
                { value: '2', label: m.camera_opt_latency_ultra() },
              ]}
              value={String(v.options.latency)}
              onchange={(value) => void v.setOptions({ latency: value === '2' ? 2 : 1 })}
              testid="camera-opt-latency"
            />
          {/if}
          <Switch
            id="camera-opt-fallback"
            label={m.camera_opt_fallback()}
            hint={m.camera_opt_fallback_hint()}
            states={{ on: m.switch_on(), off: m.switch_off() }}
            checked={v.options.fallback}
            onchange={(checked) => void v.setOptions({ fallback: checked })}
            testid="camera-opt-fallback"
          />
          <Switch
            id="camera-opt-saver"
            label={m.camera_opt_saver()}
            hint={m.camera_opt_saver_hint()}
            states={{ on: m.switch_on(), off: m.switch_off() }}
            checked={v.options.dataSaver}
            onchange={(checked) => void v.setOptions({ dataSaver: checked })}
            testid="camera-opt-saver"
          />
          <Switch
            id="camera-opt-rejoin"
            label={m.camera_opt_rejoin()}
            hint={m.camera_opt_rejoin_hint()}
            states={{ on: m.switch_on(), off: m.switch_off() }}
            checked={v.options.autoRejoin}
            onchange={(checked) => void v.setOptions({ autoRejoin: checked })}
            testid="camera-opt-rejoin"
          />
        </div>
      </details>
    {/if}
  {/if}
</div>

<style>
  .camera {
    display: grid;
    gap: var(--md-space-2);
  }

  .head,
  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2);
  }

  .head {
    justify-content: flex-end;
  }

  .controls:empty {
    display: none;
  }

  .picker {
    display: inline-grid;
    grid-auto-columns: minmax(0, 1fr);
    grid-auto-flow: column;
    max-width: 100%;
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    overflow: hidden;
  }

  .picker button {
    min-height: var(--md-target);
    padding: 0 var(--md-space-3);
    border: 0;
    border-inline-start: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-weight: var(--md-weight-medium);
    cursor: pointer;
  }

  .picker button:first-child {
    border-inline-start: 0;
  }

  .picker button[aria-checked='true'] {
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .mono {
    font-family: var(--md-font-mono);
  }

  .fold summary {
    min-height: var(--md-target);
    display: flex;
    align-items: center;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
    cursor: pointer;
  }

  .readout {
    display: grid;
    gap: var(--md-space-1);
    font-size: var(--md-font-size-12);
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }

  .event {
    color: var(--md-color-ink-muted);
  }

  .options {
    display: grid;
    gap: var(--md-space-2);
    padding-bottom: var(--md-space-2);
  }
</style>

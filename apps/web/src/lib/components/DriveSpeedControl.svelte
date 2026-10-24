<!--
  "Fahrgeschwindigkeit" in the mower settings (ADR 0042): the vendor's speed setting for manual
  driving, by which the mower scales the stick. The big readout shows the reported value
  (`MowerState.drive_speed_mps`, or "unbekannt"); the slider runs over the capability's range on
  its grid and sends `set_drive_speed` once on release, not on every input event. Locked while a
  spot run or a calibration drives the mower, or while a change is on its way.
-->
<script lang="ts">
  import { Icon } from '@mowdeck/ui';

  import {
    driveSpeedCommand,
    formatDriveSpeed,
    snapDriveSpeed,
    type DriveSpeedSetting,
  } from '#lib/mower/drive-calibration.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    setting: DriveSpeedSetting;
    /** `MowerState.drive_speed_mps`. */
    current: number | null;
    /** A spot run or a calibration is active. */
    locked: boolean;
    /** A `set_drive_speed` is on its way. */
    pending: boolean;
    allowed: boolean;
    onset: (command: { kind: 'set_drive_speed'; speed_mps: number }) => void;
  }
  let { setting, current, locked, pending, allowed, onset }: Props = $props();

  const uid = $props.id();
  /** The value under the thumb while dragging; null follows the reported value. */
  let dragging = $state<number | null>(null);

  const shown = $derived(dragging ?? (current == null ? null : snapDriveSpeed(current, setting)));
  const position = $derived(shown ?? setting.min_mps);
  const disabled = $derived(locked || pending || !allowed);
  const span = $derived(Math.max(setting.max_mps - setting.min_mps, 1e-9));
  const fill = $derived(((position - setting.min_mps) / span) * 100);

  function input(event: Event): void {
    dragging = snapDriveSpeed(Number((event.currentTarget as HTMLInputElement).value), setting);
  }

  function release(event: Event): void {
    const value = Number((event.currentTarget as HTMLInputElement).value);
    dragging = null;
    const command = driveSpeedCommand(value, current, setting);
    if (command) onset(command);
  }
</script>

<section class="speed" aria-labelledby="{uid}-title" data-testid="drive-speed">
  <div class="head">
    <h3 class="label" id="{uid}-title">
      <Icon name="speed" size={16} />
      {m.drive_speed_title()}
    </h3>
    <output
      class="mono value"
      for="{uid}-range"
      data-unknown={shown == null ? 'true' : undefined}
      data-testid="drive-speed-value"
      aria-busy={pending ? 'true' : undefined}>{formatDriveSpeed(shown, setting)}</output
    >
  </div>
  <div class="slider" style:--fill="{fill}%">
    <input
      id="{uid}-range"
      type="range"
      min={setting.min_mps}
      max={setting.max_mps}
      step={setting.step_mps}
      value={position}
      {disabled}
      aria-label={m.drive_speed_title()}
      aria-valuetext={formatDriveSpeed(shown, setting)}
      data-testid="drive-speed-range"
      oninput={input}
      onchange={release}
    />
    <div class="scale mono" aria-hidden="true">
      <span>{formatDriveSpeed(setting.min_mps, setting)}</span>
      <span>{formatDriveSpeed(setting.max_mps, setting)}</span>
    </div>
  </div>
  <p class="hint" data-testid="drive-speed-hint">
    {locked ? m.drive_speed_busy() : allowed ? m.drive_speed_hint() : m.calib_not_allowed()}
  </p>
</section>

<style>
  .speed {
    display: grid;
    gap: var(--md-space-2);
  }

  .head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--md-space-3);
  }

  .label {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .value {
    font-size: var(--md-font-size-20);
    line-height: var(--md-line-28);
  }

  .value[data-unknown='true'] {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .value[aria-busy='true'] {
    opacity: 0.6;
  }

  /* The slider sits on a sunken track that fills in ink up to the thumb. */
  .slider {
    display: grid;
    gap: var(--md-space-0_5);
    padding: var(--md-space-2) var(--md-space-3);
    background:
      linear-gradient(
          to right,
          color-mix(in srgb, var(--md-color-ink) 8%, transparent) var(--fill),
          transparent var(--fill)
        )
        no-repeat,
      var(--md-color-surface-sunken);
  }

  .slider input {
    width: 100%;
    height: var(--md-target);
    margin: 0;
    accent-color: var(--md-color-ink);
    cursor: pointer;
  }

  .slider input:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }

  .scale {
    display: flex;
    justify-content: space-between;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
  }

  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }
</style>

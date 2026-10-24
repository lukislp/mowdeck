<!--
  The state of an RTK base station as a linked mower reports it (`MowerState.base_station`):
  how the corrections reach the mower (LoRa with its channel, internet or network RTK) and whether
  the mower receives them, the base's satellites and solution, its Wi-Fi signal and position, and
  a warning when it was moved. Instrument tiles like the mower page; the line below names the
  mower that reported it and when.
-->
<script lang="ts">
  import { formatLatLon, type BaseStationState } from '$lib/fleet/base-stations';
  import { correctionLabel, solutionLabel } from '$lib/fleet/base-station-labels';
  import { formatAge, formatNumber } from '$lib/i18n';
  import { m } from '$lib/paraglide/messages';

  interface Props {
    state: BaseStationState | null;
    /** The mower that reported it (or the one being waited for). */
    mowerName: string | null;
    now: number;
  }
  let { state, mowerName, now }: Props = $props();

  const DBM = 'dBm';

  const solutionTone = $derived(
    state?.rtk_status === 'fixed'
      ? 'mowing'
      : state?.rtk_status === 'float' || state?.rtk_status === 'single'
        ? 'paused'
        : state?.rtk_status === 'none'
          ? 'error'
          : null,
  );
  const linkTone = $derived(
    state?.link_up === true ? 'mowing' : state?.link_up === false ? 'error' : null,
  );
</script>

{#if !state}
  <p class="hint" data-testid="bs-state-empty">
    {mowerName ? m.bs_state_waiting({ mower: mowerName }) : m.bs_state_none()}
  </p>
{:else}
  <div class="tiles" data-testid="bs-state">
    <div class="tile" data-testid="bs-correction" data-link={state.correction_link ?? 'unknown'}>
      <span class="label">{m.bs_correction()}</span>
      <span class="value">{correctionLabel(state.correction_link)}</span>
      {#if state.lora_channel != null}
        <span class="sub mono">{m.bs_channel({ channel: formatNumber(state.lora_channel) })}</span>
      {/if}
    </div>
    <div class="tile" data-testid="bs-link" data-up={state.link_up ?? 'unknown'}>
      <span class="label">{m.bs_reception()}</span>
      <span class="value lamp-row" data-tone={linkTone}>
        <span class="lamp" aria-hidden="true"></span>
        {state.link_up === true
          ? m.bs_link_up()
          : state.link_up === false
            ? m.bs_link_down()
            : m.no_data()}
      </span>
    </div>
    <div class="tile" data-testid="bs-satellites">
      <span class="label">{m.bs_satellites()}</span>
      <span class="value mono big"
        >{state.satellites != null ? formatNumber(state.satellites) : '—'}</span
      >
    </div>
    <div class="tile" data-testid="bs-solution">
      <span class="label">{m.bs_solution()}</span>
      <span class="value mono" data-tone={solutionTone}>{solutionLabel(state.rtk_status)}</span>
    </div>
    <div class="tile" data-testid="bs-wifi">
      <span class="label">{m.bs_wifi()}</span>
      <span class="value mono"
        >{state.wifi_dbm != null
          ? formatNumber(state.wifi_dbm)
          : '—'}{#if state.wifi_dbm != null}<span class="unit">{DBM}</span>{/if}</span
      >
    </div>
    <div class="tile" data-testid="bs-position">
      <span class="label">{m.bs_position()}</span>
      <span class="value mono small">{state.position ? formatLatLon(state.position) : '—'}</span>
    </div>
  </div>
  {#if state.moved}
    <p class="moved" data-testid="bs-moved">{m.bs_moved()}</p>
  {/if}
  {#if mowerName}
    <p class="by mono" data-testid="bs-reported-by">
      {m.bs_reported_by({
        mower: mowerName,
        age: state.details_at ? formatAge(state.details_at, now) : '—',
      })}
    </p>
  {/if}
{/if}

<style>
  .tiles {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--md-space-0_5);
  }

  .tile {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    min-width: 0;
    padding: var(--md-space-2) var(--md-space-3);
    background: var(--md-color-surface-sunken);
  }

  .label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .value {
    font-size: var(--md-font-size-16);
    line-height: var(--md-line-24);
    overflow-wrap: anywhere;
  }

  .mono,
  .unit {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .big {
    font-size: var(--md-font-size-24);
    line-height: var(--md-line-28);
  }

  .small {
    font-size: var(--md-font-size-14);
  }

  .unit {
    margin-left: var(--md-space-1);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .sub {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .lamp-row {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
  }

  .lamp {
    width: 8px;
    height: 8px;
    border-radius: var(--md-radius-round);
    background: currentcolor;
  }

  [data-tone='mowing'] {
    color: var(--md-state-mowing);
  }

  [data-tone='paused'] {
    color: var(--md-state-paused);
  }

  [data-tone='error'] {
    color: var(--md-state-error);
  }

  .hint,
  .by {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .by {
    margin-top: var(--md-space-2);
    font-size: var(--md-font-size-12);
  }

  .moved {
    margin: var(--md-space-2) 0 0;
    padding: var(--md-space-2) var(--md-space-3);
    border-left: 3px solid var(--md-state-paused);
    background: var(--md-state-paused-bg);
    font-size: var(--md-font-size-14);
  }

  @media (min-width: 768px) {
    .tiles {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
  }
</style>

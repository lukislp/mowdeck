<!--
  Reception and health of the mower, compact: the RTK solution with satellites and the age of
  the corrections (`MowerState.gnss`), the Wi-Fi and mobile signal in dBm with a four-bar glyph
  and the uplink in use (`signal`), the start self-check with a readable reason when it blocks
  (`self_check`) and the rain sensor (`rain_detected`). Tiles the mower does not report are left
  out; a stale mower (no live link) dims the values, they are the last ones it reported.
-->
<script lang="ts">
  import { formatNumber } from '$lib/i18n';
  import {
    SIGNAL_BARS,
    correctionAge,
    correctionTone,
    gnssTone,
    signalBars,
    signalTone,
    type GnssStatus,
    type SelfCheck,
    type SignalStatus,
  } from '$lib/mower/reception';
  import { gnssSolutionLabel, networkLabel, selfCheckReason } from '$lib/mower/reception-labels';
  import { m } from '$lib/paraglide/messages';

  interface Props {
    gnss: GnssStatus | null;
    signal: SignalStatus | null;
    selfCheck: SelfCheck | null;
    rainDetected: boolean | null;
    stale?: boolean;
  }
  let { gnss, signal, selfCheck, rainDetected, stale = false }: Props = $props();

  /** Units are symbols, the same in every language. */
  const DBM = 'dBm';

  const solution = $derived(gnss?.rtk_status ?? null);
  const age = $derived(
    typeof gnss?.correction_age_s === 'number' ? correctionAge(gnss.correction_age_s) : null,
  );
  const wifiBars = $derived(signalBars(signal?.wifi_dbm, 'wifi'));
  const mobileBars = $derived(signalBars(signal?.mobile_dbm, 'mobile'));
  const reason = $derived(selfCheck ? selfCheckReason(selfCheck) : null);
</script>

{#snippet glyph(bars: number | null, label: string)}
  <span
    class="glyph"
    role="img"
    aria-label={bars === null ? label : `${label}: ${m.rx_bars({ bars })}`}
    data-bars={bars ?? 'none'}
    data-tone={signalTone(bars)}
  >
    {#each { length: SIGNAL_BARS }, i (i)}
      <span class="bar b{i + 1}" class:on={bars !== null && i < bars}></span>
    {/each}
  </span>
{/snippet}

<div class="rx" class:stale data-testid="reception-panel">
  {#if gnss}
    <div class="tile wide" data-testid="rx-gnss">
      <span class="label">{m.rx_rtk()}</span>
      <div class="gnss">
        <span class="value" data-tone={gnssTone(solution)} data-testid="rx-solution">
          {solution ? gnssSolutionLabel(solution) : m.no_data()}
        </span>
        <span class="sub">
          <span class="pair" data-testid="rx-satellites">
            <span class="num">{gnss.satellites != null ? formatNumber(gnss.satellites) : '—'}</span>
            <span class="key">{m.rx_satellites()}</span>
          </span>
          <span
            class="pair"
            data-tone={correctionTone(gnss.correction_age_s)}
            data-testid="rx-correction"
          >
            <span class="num"
              >{age ? `${formatNumber(age.value, age.digits)} ${age.unit}` : '—'}</span
            >
            <span class="key">{m.rx_correction()}</span>
          </span>
        </span>
      </div>
    </div>
  {/if}

  {#if signal}
    <div class="tile" data-testid="rx-wifi">
      <span class="label">{m.rx_wifi()}</span>
      <span class="radio">
        {@render glyph(wifiBars, m.rx_wifi())}
        <span class="num">{signal.wifi_dbm != null ? formatNumber(signal.wifi_dbm) : '—'}</span>
        {#if signal.wifi_dbm != null}<span class="unit">{DBM}</span>{/if}
      </span>
    </div>
    <div class="tile" data-testid="rx-mobile">
      <span class="label">{m.rx_mobile()}</span>
      <span class="radio">
        {@render glyph(mobileBars, m.rx_mobile())}
        <span class="num">{signal.mobile_dbm != null ? formatNumber(signal.mobile_dbm) : '—'}</span>
        {#if signal.mobile_dbm != null}<span class="unit">{DBM}</span>{/if}
      </span>
    </div>
    <div class="tile" data-testid="rx-network">
      <span class="label">{m.rx_network()}</span>
      <span class="word">{signal.network ? networkLabel(signal.network) : m.no_data()}</span>
    </div>
  {/if}

  {#if typeof rainDetected === 'boolean'}
    <div class="tile" data-testid="rx-rain" data-rain={rainDetected ? 'wet' : 'dry'}>
      <span class="label">{m.rx_rain()}</span>
      <span class="word" data-tone={rainDetected ? 'paused' : null}>
        {rainDetected ? m.rx_rain_wet() : m.rx_rain_dry()}
      </span>
    </div>
  {/if}

  {#if selfCheck}
    <div class="tile wide check" data-testid="rx-self-check" data-result={selfCheck.result}>
      <span class="label">{m.rx_self_check()}</span>
      <span class="word" data-tone={selfCheck.result === 'ok' ? 'mowing' : 'error'}>
        <span class="lamp" aria-hidden="true"></span>
        {selfCheck.result === 'ok' ? m.rx_check_ok() : m.rx_check_blocked()}
      </span>
      {#if reason}
        <span class="reason" data-testid="rx-self-check-reason">{reason}</span>
      {/if}
    </div>
  {/if}
</div>

<style>
  .rx {
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

  .wide {
    grid-column: 1 / -1;
  }

  .label,
  .key {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .key {
    font-size: var(--md-font-size-11);
  }

  .gnss {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--md-space-1) var(--md-space-4);
  }

  .value {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-20);
    line-height: var(--md-line-28);
  }

  .sub {
    display: flex;
    gap: var(--md-space-4);
  }

  .pair {
    display: flex;
    align-items: baseline;
    gap: var(--md-space-1);
  }

  .num,
  .word {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-16);
    line-height: var(--md-line-24);
    font-variant-numeric: tabular-nums;
  }

  .unit {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
  }

  .radio {
    display: flex;
    align-items: baseline;
    gap: var(--md-space-1);
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

  /* Four bars rising to the right, on the text baseline; unlit bars are hairline ghosts. */
  .glyph {
    display: inline-flex;
    align-items: flex-end;
    gap: 2px;
    height: 14px;
    margin-right: var(--md-space-1);
    align-self: center;
    color: var(--md-color-ink);
  }

  .glyph[data-tone='paused'] {
    color: var(--md-state-paused);
  }

  .glyph[data-tone='error'] {
    color: var(--md-state-error);
  }

  .bar {
    width: 4px;
    background: var(--md-color-line);
  }

  .bar.on {
    background: currentcolor;
  }

  .b1 {
    height: 25%;
  }

  .b2 {
    height: 50%;
  }

  .b3 {
    height: 75%;
  }

  .b4 {
    height: 100%;
  }

  .check .word {
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

  .reason {
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .stale .num,
  .stale .word,
  .stale .value,
  .stale .glyph {
    opacity: 0.6;
  }
</style>

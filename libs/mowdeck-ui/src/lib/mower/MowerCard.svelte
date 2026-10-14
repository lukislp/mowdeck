<!--
  One row of the mower list (72 px, no card chrome): name and connection on the left, activity,
  battery and RTK as aligned columns, last update age. The whole row is one link.
-->
<script lang="ts">
  import Icon from '../icons/Icon.svelte';
  import StatusBadge from '../status/StatusBadge.svelte';
  import type { Activity, RtkFix } from '../types.js';

  interface Props {
    href: string;
    name: string;
    /** Secondary line, e.g. the connection kind. */
    detail: string;
    activity: Activity;
    activityLabel: string;
    /** Formatted battery value ("87") or null. */
    battery: string | null;
    charging?: boolean;
    rtk: RtkFix;
    rtkLabel: string;
    /** "vor 12 s", or the empty text. */
    age: string;
    stale?: boolean;
    /** An error line under the name (error code and message). */
    error?: string | null;
    testid?: string;
  }
  let {
    href,
    name,
    detail,
    activity,
    activityLabel,
    battery,
    charging = false,
    rtk,
    rtkLabel,
    age,
    stale = false,
    error = null,
    testid,
  }: Props = $props();
</script>

<a class="md-mower-row" class:stale {href} data-testid={testid} data-activity={activity}>
  <span class="ident">
    <span class="name">{name}</span>
    {#if error}
      <span class="error"><Icon name="alert" size={14} />{error}</span>
    {:else}
      <span class="detail">{detail}</span>
    {/if}
  </span>
  <span class="badge"><StatusBadge {activity} label={activityLabel} {stale} /></span>
  <span class="battery" class:low={battery !== null && Number(battery) < 20}>
    {#if charging}<Icon name="bolt" size={14} />{/if}
    <span class="mono">{battery ?? '--'}</span><span class="unit">%</span>
  </span>
  <span class="rtk" data-rtk={rtk}>
    <span class="bars" aria-hidden="true"><i></i><i></i><i></i></span>
    <span class="rtk-label">{rtkLabel}</span>
  </span>
  <span class="age mono">{age}</span>
  <span class="go" aria-hidden="true"><Icon name="chevron" size={18} /></span>
</a>

<style>
  .md-mower-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto 20px;
    grid-template-areas:
      'ident badge battery go'
      'ident rtk age go';
    align-items: center;
    gap: var(--md-space-1) var(--md-space-3);
    min-height: 72px;
    padding: var(--md-space-3) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    text-decoration: none;
  }

  .md-mower-row:hover {
    background: var(--md-color-surface-sunken);
  }

  .md-mower-row:focus-visible {
    outline-offset: -2px;
  }

  .md-mower-row.stale {
    background: var(--md-color-bg);
  }

  .ident {
    grid-area: ident;
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .name {
    overflow: hidden;
    font-weight: var(--md-weight-semibold);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .detail {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .error {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    color: var(--md-state-error);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .badge {
    grid-area: badge;
    justify-self: end;
  }

  .battery {
    grid-area: battery;
    display: inline-flex;
    align-items: baseline;
    justify-content: flex-end;
    gap: 2px;
    min-width: 4.5ch;
  }

  .battery.low {
    color: var(--md-state-error);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums slashed-zero;
    font-weight: var(--md-weight-medium);
  }

  .unit {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
  }

  .rtk {
    grid-area: rtk;
    display: inline-flex;
    align-items: center;
    justify-content: flex-end;
    gap: var(--md-space-1);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    text-transform: uppercase;
    letter-spacing: var(--md-label-tracking);
  }

  .bars {
    display: inline-flex;
    align-items: flex-end;
    gap: 1px;
    height: 12px;
  }

  .bars i {
    width: 3px;
    background: var(--md-color-line);
  }

  .bars i:nth-child(1) {
    height: 4px;
  }

  .bars i:nth-child(2) {
    height: 8px;
  }

  .bars i:nth-child(3) {
    height: 12px;
  }

  [data-rtk='fixed'] .bars i {
    background: var(--md-rtk-fixed);
  }

  [data-rtk='float'] .bars i:nth-child(-n + 2) {
    background: var(--md-rtk-float);
  }

  [data-rtk='none'] .bars i:nth-child(1) {
    background: var(--md-rtk-none);
  }

  .age {
    grid-area: age;
    justify-self: end;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-regular);
  }

  .go {
    grid-area: go;
    color: var(--md-color-ink-muted);
  }

  @media (min-width: 768px) {
    .md-mower-row {
      grid-template-columns: minmax(0, 1fr) 150px 80px 120px 110px 20px;
      grid-template-areas: 'ident badge battery rtk age go';
    }

    .badge {
      justify-self: start;
    }
  }
</style>

<!--
  The last known position as text, for the police or anyone who has to find the mower even
  where no map layer covers the place: decimal latitude and longitude (6 places, dot decimal),
  the time of the fix and its age, a copy button, and links that open the place in Apple Maps,
  Google Maps, OpenStreetMap and (on Android) the device's maps app. The links are built from
  the coordinates alone, so a share token never ends up in them. Used by the owner's tracking
  view and the public share page. The vendor reports no accuracy figure, so none is shown.
-->
<script lang="ts">
  import { Icon } from '@mowdeck/ui';

  import { formatAge, formatDateTime } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';

  import { coordinatesText, mapLinks } from './basemap.js';

  interface Props {
    point: { lat: number; lon: number; at: string };
    /** Now, on the server's clock (for the age). */
    now: number;
    testid?: string;
  }

  let { point, now, testid = 'position-fix' }: Props = $props();

  let copied = $state(false);
  let field: HTMLInputElement | undefined = $state();

  const text = $derived(coordinatesText(point));
  const links = $derived(mapLinks(point, m.tracking_maps_label()));

  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
      setTimeout(() => (copied = false), 2500);
    } catch {
      // No clipboard (insecure context, denied): select the text for a manual copy.
      field?.focus();
      field?.select();
    }
  }
</script>

<section class="fix" data-testid={testid} aria-label={m.tracking_fix_title()}>
  <div class="head">
    <span class="label">{m.tracking_fix_title()}</span>
    <span class="time">
      {m.tracking_fix_time({ time: formatDateTime(point.at), age: formatAge(point.at, now) })}
    </span>
  </div>
  <input
    bind:this={field}
    class="value"
    readonly
    value={text}
    aria-label={m.tracking_fix_coordinates()}
    data-testid="{testid}-coordinates"
    onfocus={(event) => event.currentTarget.select()}
  />
  <div class="actions">
    <button
      type="button"
      class="copy"
      data-testid="{testid}-copy"
      aria-live="polite"
      onclick={copy}
    >
      <Icon name={copied ? 'check' : 'copy'} size={18} />
      <span>{copied ? m.tracking_fix_copied() : m.tracking_fix_copy()}</span>
    </button>
  </div>
  <ul class="links">
    <li>
      <a href={links.apple} target="_blank" rel="noopener noreferrer" data-testid="{testid}-apple"
        >{m.tracking_open_apple()}</a
      >
    </li>
    <li>
      <a href={links.google} target="_blank" rel="noopener noreferrer" data-testid="{testid}-google"
        >{m.tracking_open_google()}</a
      >
    </li>
    <li>
      <a href={links.osm} target="_blank" rel="noopener noreferrer" data-testid="{testid}-osm"
        >{m.tracking_open_osm()}</a
      >
    </li>
    {#if links.geo}
      <li>
        <a href={links.geo} rel="noopener noreferrer" data-testid="{testid}-geo"
          >{m.tracking_open_maps()}</a
        >
      </li>
    {/if}
  </ul>
</section>

<style>
  .fix {
    display: grid;
    gap: var(--md-space-3);
    padding: var(--md-space-4);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-inline-start: 4px solid var(--md-stop-fill);
    border-radius: var(--md-radius-3);
    background: var(--md-color-surface);
  }

  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--md-space-1) var(--md-space-3);
  }

  .label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .time {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .actions {
    display: flex;
  }

  .value {
    box-sizing: border-box;
    inline-size: 100%;
    min-block-size: var(--md-target);
    padding: 0 var(--md-space-3);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface-sunken);
    color: var(--md-color-ink);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-20);
    font-weight: var(--md-weight-semibold);
    font-variant-numeric: tabular-nums;
  }

  .copy {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-2);
    min-block-size: var(--md-target);
    padding: 0 var(--md-space-3);
    border: 0;
    border-radius: var(--md-radius-2);
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
    font: inherit;
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-semibold);
    cursor: pointer;
    white-space: nowrap;
  }

  .links {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .links a {
    display: inline-flex;
    align-items: center;
    min-block-size: var(--md-target);
    padding: 0 var(--md-space-3);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-round);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-semibold);
    text-decoration: none;
  }

  .value:focus-visible,
  .copy:focus-visible,
  .links a:focus-visible {
    outline: 3px solid var(--md-color-focus);
    outline-offset: 2px;
  }
</style>

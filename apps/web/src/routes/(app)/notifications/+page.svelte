<script lang="ts">
  import { Button, Icon, ProblemNotice, TopBar } from '@mowdeck/ui';

  import { goto } from '$app/navigation';

  import { fleetApi, unwrap } from '$lib/api/client';
  import { asProblem } from '$lib/api/problem';
  import NotificationBadge from '$lib/components/NotificationBadge.svelte';
  import SlowLoading from '$lib/components/SlowLoading.svelte';
  import { formatAge, problemMessage } from '$lib/i18n';
  import { clock } from '$lib/clock.svelte';
  import { alarmPosition, isAlarm } from '$lib/notifications/kinds';
  import { inbox } from '$lib/notifications/runtime';
  import { m } from '$lib/paraglide/messages';
  import { getLocale } from '$lib/paraglide/runtime';
  import { safePath } from '$lib/push/payload';
  import type { InboxItem } from '$lib/stores/inbox.svelte';
  import { toasts } from '$lib/stores/ui.svelte';

  let names = $state<Record<string, string>>({});
  let marking = $state(false);
  let acking = $state<string | null>(null);

  const problem = $derived(inbox.error ? asProblem(inbox.error) : null);

  $effect(() => {
    void inbox.refresh();
    const stop = inbox.start();
    const release = clock.hold(30_000);
    void unwrap(fleetApi.GET('/api/v1/fleet/mowers'))
      .then((list) => (names = Object.fromEntries(list.items.map((x) => [x.id, x.name]))))
      .catch(() => undefined);
    return () => {
      stop();
      release();
    };
  });

  function dayKey(at: string): string {
    const date = new Date(at);
    return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
  }

  function dayLabel(at: string): string {
    const date = new Date(at);
    const today = new Date(clock.now);
    const yesterday = new Date(clock.now - 86_400_000);
    if (dayKey(at) === dayKey(today.toISOString())) return m.notif_today();
    if (dayKey(at) === dayKey(yesterday.toISOString())) return m.notif_yesterday();
    return new Intl.DateTimeFormat(getLocale(), { dateStyle: 'full' }).format(date);
  }

  function time(at: string): string {
    return new Intl.DateTimeFormat(getLocale(), { timeStyle: 'short' }).format(new Date(at));
  }

  const days = $derived.by(() => {
    const result: { key: string; label: string; items: InboxItem[] }[] = [];
    for (const item of inbox.items) {
      const key = dayKey(item.occurred_at);
      const last = result.at(-1);
      if (last?.key === key) last.items.push(item);
      else result.push({ key, label: dayLabel(item.occurred_at), items: [item] });
    }
    return result;
  });

  async function open(item: InboxItem): Promise<void> {
    void inbox.markRead(item.id).catch(() => undefined);
    if (item.url || item.mower_id) await goto(safePath(item.url, item.mower_id));
  }

  async function acknowledge(item: InboxItem): Promise<void> {
    acking = item.id;
    try {
      await inbox.acknowledge(item.id);
    } catch (error) {
      const problem = asProblem(error);
      toasts.push(
        'error',
        m.notif_ack_failed(),
        problemMessage(problem),
        problem.correlationId ?? undefined,
      );
    } finally {
      acking = null;
    }
  }

  async function markAll(): Promise<void> {
    marking = true;
    try {
      await inbox.markAllRead();
    } finally {
      marking = false;
    }
  }
</script>

<svelte:head><title>{m.notif_inbox_title()} · {m.app_name()}</title></svelte:head>

<TopBar
  title={m.notif_inbox_title()}
  meta={inbox.loaded ? m.notif_unread_count({ count: inbox.unread }) : undefined}
>
  {#snippet actions()}
    <Button
      variant="ghost"
      icon="check"
      pending={marking}
      disabled={inbox.unread === 0}
      testid="inbox-mark-all"
      onclick={markAll}>{m.notif_mark_all()}</Button
    >
    <Button
      href="/settings#notifications"
      icon="settings"
      label={m.notif_settings_title()}
      testid="notification-settings-link"
    />
  {/snippet}
</TopBar>

{#if problem && !inbox.loaded}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if !inbox.loaded}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{:else if inbox.items.length === 0}
  <div class="empty" data-testid="inbox-empty">
    <Icon name="inbox" size={32} />
    <h2>{m.notif_empty_title()}</h2>
    <p>{m.notif_empty_body()}</p>
    <Button href="/settings#notifications" icon="settings">{m.notif_settings_title()}</Button>
  </div>
{:else}
  <div class="log" data-testid="inbox">
    {#each days as day (day.key)}
      <h2 class="day">{day.label}</h2>
      <ul>
        {#each day.items as item (item.id)}
          <li>
            <button
              type="button"
              class="item"
              class:unread={!item.read}
              data-testid="inbox-item"
              data-kind={item.kind}
              data-unread={!item.read}
              onclick={() => open(item)}
            >
              <span class="when">
                <span class="clock">{time(item.occurred_at)}</span>
                <span class="age">{formatAge(item.occurred_at, clock.now)}</span>
              </span>
              <span class="what">
                <span class="head">
                  <NotificationBadge kind={item.kind} />
                  {#if !item.read}<span class="new">{m.notif_unread()}</span>{/if}
                </span>
                <span class="title">{item.title}</span>
                {#if item.body}<span class="body">{item.body}</span>{/if}
                {#if item.mower_id}
                  <span class="mower">
                    <Icon name="mower" size={16} />{names[item.mower_id] ?? m.notif_mower()}
                  </span>
                {/if}
              </span>
              {#if item.url || item.mower_id}<Icon name="chevron" size={18} />{/if}
            </button>
            {#if isAlarm(item.kind)}
              {@const where = alarmPosition(item.url)}
              <div class="alarm" data-testid="inbox-alarm" data-acked={item.acked}>
                {#if item.acked}
                  <span class="acked" data-testid="inbox-acked">
                    <Icon name="check" size={16} />
                    {item.acked_at
                      ? m.notif_acked_at({ time: time(item.acked_at) })
                      : m.notif_acked()}
                  </span>
                {:else}
                  <Button
                    variant="primary"
                    icon="shield"
                    pending={acking === item.id}
                    testid="inbox-ack"
                    onclick={() => acknowledge(item)}>{m.notif_ack()}</Button
                  >
                  <span class="ack-hint">{m.notif_ack_hint()}</span>
                {/if}
                {#if where}
                  <a
                    class="where"
                    href={safePath(item.url, item.mower_id)}
                    data-testid="inbox-position"
                    aria-label={m.notif_position_map()}
                  >
                    <Icon name="pin" size={16} />
                    <span class="mono">{m.notif_position(where)}</span>
                  </a>
                {/if}
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/each}
  </div>
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .quiet {
    color: var(--md-color-ink-muted);
  }

  .empty {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--md-space-2);
    max-width: 480px;
    padding: var(--md-space-8) var(--md-gutter);
    color: var(--md-color-ink-muted);
  }

  .empty h2 {
    margin: var(--md-space-2) 0 0;
    color: var(--md-color-ink);
    font-size: var(--md-font-size-20);
    line-height: var(--md-line-28);
  }

  .empty p {
    margin: 0 0 var(--md-space-2);
  }

  .log {
    max-width: 880px;
    padding-bottom: var(--md-space-8);
  }

  .day {
    position: sticky;
    top: var(--md-sticky-top);
    z-index: 1;
    margin: 0;
    padding: var(--md-space-2) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-bg);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  ul {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .item {
    position: relative;
    display: grid;
    grid-template-columns: 64px minmax(0, 1fr) auto;
    align-items: start;
    gap: var(--md-space-3);
    width: 100%;
    min-height: var(--md-target);
    padding: var(--md-space-3) var(--md-gutter);
    border: 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
    color: var(--md-color-ink-muted);
    font: inherit;
    text-align: start;
    cursor: pointer;
  }

  .item:hover {
    background: var(--md-color-surface-sunken);
  }

  .item.unread::before {
    content: '';
    position: absolute;
    inset-block: 0;
    inset-inline-start: 0;
    width: 3px;
    background: var(--md-color-ink);
  }

  .when {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-0_5);
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .clock {
    color: var(--md-color-ink);
    font-size: var(--md-font-size-16);
    font-weight: var(--md-weight-medium);
  }

  .age {
    font-size: var(--md-font-size-11);
  }

  .what {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    min-width: 0;
  }

  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2);
  }

  .new {
    color: var(--md-color-ink);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-11);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .title {
    color: var(--md-color-ink);
    font-weight: var(--md-weight-medium);
  }

  .unread .title {
    font-weight: var(--md-weight-semibold);
  }

  .body {
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .mower {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
  }

  .item > :global(svg) {
    align-self: center;
  }

  /* Alarm actions sit under the text column of their item. */
  .alarm {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2) var(--md-space-3);
    padding: 0 var(--md-gutter) var(--md-space-3) calc(var(--md-gutter) + 64px + var(--md-space-3));
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  li:has(.alarm) .item {
    border-bottom: 0;
  }

  .acked {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    min-height: var(--md-target);
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .ack-hint {
    flex-basis: 100%;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  .where {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    min-height: var(--md-target);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
  }

  .where:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }
</style>

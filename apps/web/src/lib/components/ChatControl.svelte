<!--
  Chat control (ADR 0075): a command line on the mower page. A typed request becomes a work order
  from the optional assistant add-on; nothing runs until the user confirms it, and then every
  action goes through the regular command and schedule endpoints with the user's token
  (`#lib/assistant/assistant.js`). The page shows this only when the add-on announces its UI area.
  Free text from a model is never shown: the card is built from validated, structured actions.
-->
<script lang="ts">
  import { Button, Icon } from '@mowdeck/ui';

  import { schedulerApi, unwrap } from '#lib/api/client.js';
  import { asProblem } from '#lib/api/problem.js';
  import {
    TEXT_MAX,
    actionLabel,
    areaSummary,
    blockedText,
    browserTimeZone,
    examples,
    execute,
    expired,
    formatLocal,
    loadStatus,
    noteText,
    propose,
    rejectionText,
    type AssistantStatus,
    type Proposal,
    type ProposedAction,
  } from '#lib/assistant/assistant.js';
  import { problemMessage } from '#lib/i18n.js';
  import { sendCommand } from '#lib/live.svelte.js';
  import { m } from '#lib/paraglide/messages.js';
  import { getLocale } from '#lib/paraglide/runtime.js';
  import type { Locale } from '#lib/schedules/types.js';
  import { toasts } from '#lib/stores/ui.svelte.js';
  import { PROVIDER_SETTINGS_ROUTE } from '#lib/vision/api.js';

  interface Props {
    mowerId: string;
    /** After a confirmed order ran (the page reloads its next schedule and commands). */
    ondone?: () => void;
  }
  let { mowerId, ondone }: Props = $props();

  type Gate = { kind: 'loading' } | { kind: 'hidden' } | { kind: 'ok'; status: AssistantStatus };

  let gate = $state<Gate>({ kind: 'loading' });
  let text = $state('');
  let asked = $state('');
  let busy = $state(false);
  let running = $state(false);
  let error = $state<string | null>(null);
  let proposal = $state.raw<Proposal | null>(null);
  const locale = $derived(getLocale() as Locale);

  $effect(() => {
    void mowerId;
    proposal = null;
    error = null;
    void loadStatus()
      .then((status) => (gate = { kind: 'ok', status }))
      // Not deployed or not reachable: no chat box at all.
      .catch(() => (gate = { kind: 'hidden' }));
  });

  const status = $derived(gate.kind === 'ok' ? gate.status : null);
  const source = $derived.by(() => {
    if (!status) return '';
    if (status.interpreter === 'phrases') return m.chat_source_phrases();
    if (!status.model) return '';
    return status.hosted
      ? m.chat_source_hosted({ model: status.model })
      : m.chat_source_ai({ model: status.model });
  });

  async function ask(request = text): Promise<void> {
    const value = request.trim();
    if (!value || busy) return;
    text = value;
    asked = value;
    busy = true;
    error = null;
    proposal = null;
    try {
      proposal = await propose(mowerId, value.slice(0, TEXT_MAX), browserTimeZone());
    } catch (failure) {
      error = m.chat_error({ message: problemMessage(asProblem(failure)) });
    } finally {
      busy = false;
    }
  }

  function discard(): void {
    proposal = null;
    error = null;
  }

  async function confirm(): Promise<void> {
    const order = proposal;
    if (!order || running) return;
    if (expired(order, Date.now())) {
      error = m.chat_expired();
      proposal = null;
      return;
    }
    running = true;
    try {
      const result = await execute(
        order,
        {
          command: (id, command) => sendCommand(id, command),
          schedule: async (body) => {
            try {
              await unwrap(schedulerApi.POST('/api/v1/scheduler/schedules', { body }));
              return true;
            } catch (failure) {
              const problem = asProblem(failure);
              toasts.push(
                'error',
                problemMessage(problem),
                problem.detail ?? undefined,
                problem.correlationId ?? undefined,
              );
              return false;
            }
          },
        },
        Date.now(),
      );
      if (result.failedAt === null) {
        toasts.push('ok', m.chat_done({ mower: order.mower_name }));
        proposal = null;
        text = '';
        ondone?.();
      } else {
        error = m.chat_failed();
      }
    } finally {
      running = false;
    }
  }

  function tone(action: ProposedAction): string {
    switch (action.kind) {
      case 'start_now':
      case 'start_later':
        return 'mowing';
      case 'pause':
        return 'paused';
      case 'resume':
        return 'mowing';
      case 'dock':
        return 'returning';
    }
  }

  function icon(action: ProposedAction): 'play' | 'calendar' | 'pause' | 'dock' {
    switch (action.kind) {
      case 'start_now':
      case 'resume':
        return 'play';
      case 'start_later':
        return 'calendar';
      case 'pause':
        return 'pause';
      case 'dock':
        return 'dock';
    }
  }

  const blocked = $derived(proposal ? blockedText(proposal) : null);
</script>

{#if gate.kind === 'ok'}
  <section class="chat" data-testid="chat-control" aria-labelledby="chat-title">
    <header class="head">
      <span class="glyph" aria-hidden="true">&gt;_</span>
      <h2 id="chat-title">{m.chat_title()}</h2>
      {#if source}<span class="source" data-testid="chat-source">{source}</span>{/if}
    </header>

    {#if status && !status.ready}
      <div class="setup" data-testid="chat-no-provider">
        <p>
          {status.reason === 'no_provider' ? m.chat_no_provider() : m.chat_provider_unavailable()}
        </p>
        {#if status.reason === 'no_provider'}
          <Button href={PROVIDER_SETTINGS_ROUTE} icon="link" testid="chat-provider-settings"
            >{m.chat_no_provider_link()}</Button
          >
        {/if}
      </div>
    {:else}
      <p class="lead">{m.chat_lead()}</p>
      <form
        class="line"
        onsubmit={(event) => {
          event.preventDefault();
          void ask();
        }}
      >
        <span class="prompt" aria-hidden="true">›</span>
        <input
          type="text"
          bind:value={text}
          maxlength={TEXT_MAX}
          placeholder={m.chat_placeholder()}
          aria-label={m.chat_label()}
          autocomplete="off"
          enterkeyhint="send"
          data-testid="chat-input"
        />
        <button
          type="submit"
          class="go"
          disabled={busy || !text.trim()}
          aria-label={m.chat_send()}
          aria-busy={busy ? 'true' : undefined}
          data-testid="chat-send"
        >
          <Icon name="send" size={18} />
        </button>
      </form>
      {#if !proposal && !busy}
        <div class="examples" aria-label={m.chat_examples()}>
          {#each examples() as example (example)}
            <button
              type="button"
              class="example"
              data-testid="chat-example"
              onclick={() => void ask(example)}>{example}</button
            >
          {/each}
        </div>
      {/if}
      {#if busy}
        <p class="thinking" role="status" data-testid="chat-busy">{m.chat_thinking()}</p>
      {/if}
      {#if error}
        <p class="error" role="alert" data-testid="chat-error">{error}</p>
      {/if}

      {#if proposal}
        <article class="order" data-testid="chat-order" aria-labelledby="chat-order-title">
          <div class="echo"><span aria-hidden="true">›</span> {asked}</div>
          <header class="order-head">
            <h3 id="chat-order-title">{m.chat_order()}</h3>
            <span class="mower">{m.chat_mower()}: <strong>{proposal.mower_name}</strong></span>
          </header>

          {#if proposal.executable}
            <ol class="steps">
              {#each proposal.actions as action, index (index)}
                <li
                  class="step"
                  style:--step-tone="var(--md-state-{tone(action)})"
                  data-kind={action.kind}
                  data-testid="chat-action"
                >
                  <span class="no">{String(index + 1).padStart(2, '0')}</span>
                  <div class="what">
                    <strong class="verb"
                      ><Icon name={icon(action)} size={16} /> {actionLabel(action.kind)}</strong
                    >
                    <dl>
                      {#if action.area_ids.length}
                        <div>
                          <dt>{m.chat_areas()}</dt>
                          <dd data-testid="chat-areas">{areaSummary(action, proposal)}</dd>
                        </div>
                      {/if}
                      <div>
                        <dt>{m.chat_when()}</dt>
                        <dd class="mono" data-testid="chat-when">
                          {action.start_local
                            ? formatLocal(action.start_local, locale)
                            : m.chat_now()}
                        </dd>
                      </div>
                      {#if action.duration_minutes}
                        <div>
                          <dt class="sr-only">
                            {m.chat_window({ minutes: action.duration_minutes })}
                          </dt>
                          <dd class="mono muted">
                            {m.chat_window({ minutes: action.duration_minutes })}
                          </dd>
                        </div>
                      {/if}
                    </dl>
                  </div>
                </li>
              {/each}
            </ol>
          {:else}
            <p class="nothing" data-testid="chat-nothing">{m.chat_nothing()}</p>
          {/if}

          {#if blocked || proposal.rejected.length || proposal.notes.length}
            <ul class="notes" data-testid="chat-notes">
              {#if blocked}<li class="warn">{blocked}</li>{/if}
              {#each proposal.rejected as rejected, index (index)}
                <li class="warn">{rejectionText(rejected.code)}</li>
              {/each}
              {#each proposal.notes as note (note)}
                <li data-note={note}>{noteText(note)}</li>
              {/each}
            </ul>
          {/if}

          <footer class="order-foot">
            {#if proposal.executable}<p class="hint">{m.chat_order_hint()}</p>{/if}
            <div class="buttons">
              <Button variant="ghost" onclick={discard} testid="chat-discard"
                >{m.chat_discard()}</Button
              >
              {#if proposal.executable}
                <Button
                  variant="primary"
                  icon="check"
                  pending={running}
                  onclick={() => void confirm()}
                  testid="chat-confirm">{m.chat_confirm()}</Button
                >
              {/if}
            </div>
          </footer>
        </article>
      {/if}
    {/if}
  </section>
{/if}

<style>
  .chat {
    display: grid;
    gap: var(--md-space-3);
    padding: var(--md-space-4);
    border: var(--md-hairline) solid var(--md-color-line);
    border-radius: var(--md-radius-3);
    background: var(--md-color-surface);
    min-width: 0;
  }

  .head {
    display: flex;
    align-items: baseline;
    gap: var(--md-space-2);
    flex-wrap: wrap;
  }

  .glyph {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
    font-weight: var(--md-weight-semibold);
    color: var(--md-color-ink-inverse);
    background: var(--md-color-ink);
    padding: 0 var(--md-space-1);
    border-radius: var(--md-radius-1);
    line-height: var(--md-line-20);
  }

  h2 {
    margin: 0;
    font-size: var(--md-font-size-16);
    font-weight: var(--md-weight-semibold);
  }

  .source {
    margin-left: auto;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-11);
    letter-spacing: var(--md-label-tracking);
    color: var(--md-color-ink-muted);
    overflow-wrap: anywhere;
  }

  .lead,
  .setup p {
    margin: 0;
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-20);
    color: var(--md-color-ink-muted);
  }

  .setup {
    display: grid;
    gap: var(--md-space-3);
    justify-items: start;
  }

  .line {
    display: flex;
    align-items: stretch;
    min-height: var(--md-target);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface-sunken);
    transition: border-color var(--md-motion-fast) var(--md-ease);
  }

  .line:focus-within {
    border-color: var(--md-color-focus);
  }

  .prompt {
    display: grid;
    place-items: center;
    width: 36px;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-20);
    color: var(--md-state-mowing);
  }

  .line input {
    flex: 1;
    min-width: 0;
    border: 0;
    background: transparent;
    color: var(--md-color-ink);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-14);
    padding: 0 var(--md-space-2) 0 0;
    outline: none;
  }

  .line input::placeholder {
    color: var(--md-color-ink-muted);
  }

  .go {
    display: grid;
    place-items: center;
    width: var(--md-target);
    border: 0;
    border-left: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
    border-radius: var(--md-radius-0) var(--md-radius-1) var(--md-radius-1) var(--md-radius-0);
    cursor: pointer;
  }

  .go:disabled {
    background: transparent;
    color: var(--md-color-ink-muted);
    cursor: default;
  }

  .go:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .examples {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .example {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    color: var(--md-color-ink);
    background: transparent;
    border: var(--md-hairline) dashed var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    padding: var(--md-space-1) var(--md-space-2);
    min-height: 32px;
    cursor: pointer;
    text-align: left;
  }

  .example:hover {
    border-style: solid;
  }

  .example:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .thinking {
    margin: 0;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    color: var(--md-color-ink-muted);
    background: linear-gradient(
        90deg,
        transparent 0%,
        var(--md-state-mowing-bg) 50%,
        transparent 100%
      )
      0 0 / 40% 100% no-repeat;
    animation: scan 1.4s var(--md-ease) infinite;
    padding: var(--md-space-1) 0;
  }

  @keyframes scan {
    from {
      background-position: -40% 0;
    }

    to {
      background-position: 140% 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .thinking {
      animation: none;
    }
  }

  .error {
    margin: 0;
    font-size: var(--md-font-size-13);
    color: var(--md-state-error);
  }

  .order {
    display: grid;
    gap: var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-bg);
    padding: 0 var(--md-space-3) var(--md-space-3);
    min-width: 0;
  }

  .echo {
    margin: 0 calc(-1 * var(--md-space-3));
    padding: var(--md-space-2) var(--md-space-3);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
    color: var(--md-color-ink-muted);
    border-bottom: var(--md-hairline) dashed var(--md-color-line-strong);
    overflow-wrap: anywhere;
  }

  .order-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--md-space-2);
    flex-wrap: wrap;
  }

  h3 {
    margin: 0;
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    color: var(--md-color-ink-muted);
  }

  .mower {
    font-size: var(--md-font-size-13);
    color: var(--md-color-ink-muted);
  }

  .mower strong {
    color: var(--md-color-ink);
  }

  .steps {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--md-space-2);
  }

  .step {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: var(--md-space-3);
    padding: var(--md-space-3);
    background: var(--md-color-surface);
    border: var(--md-hairline) solid var(--md-color-line);
    border-left: 4px solid var(--step-tone);
    border-radius: var(--md-radius-2);
  }

  .no {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    color: var(--step-tone);
    font-weight: var(--md-weight-semibold);
    padding-top: 2px;
  }

  .what {
    display: grid;
    gap: var(--md-space-2);
    min-width: 0;
  }

  .verb {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-2);
    font-size: var(--md-font-size-16);
    font-weight: var(--md-weight-semibold);
  }

  dl {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-1) var(--md-space-4);
    margin: 0;
  }

  dl div {
    display: flex;
    gap: var(--md-space-2);
    align-items: baseline;
    min-width: 0;
  }

  dt {
    font-size: var(--md-font-size-11);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    color: var(--md-color-ink-muted);
  }

  dd {
    margin: 0;
    font-size: var(--md-font-size-14);
    overflow-wrap: anywhere;
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .muted {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  .nothing {
    margin: 0;
    font-weight: var(--md-weight-semibold);
  }

  .notes {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--md-space-1);
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-20);
  }

  .notes li {
    padding-left: var(--md-space-3);
    border-left: 2px solid var(--md-state-paused);
    color: var(--md-color-ink);
  }

  .notes li.warn {
    border-left-color: var(--md-state-error);
  }

  .order-foot {
    display: grid;
    gap: var(--md-space-2);
  }

  .hint {
    margin: 0;
    font-size: var(--md-font-size-12);
    color: var(--md-color-ink-muted);
  }

  .buttons {
    display: flex;
    justify-content: flex-end;
    gap: var(--md-space-2);
    flex-wrap: wrap;
  }

  @media (max-width: 480px) {
    .buttons {
      flex-direction: column-reverse;
      align-items: stretch;
    }
  }
</style>

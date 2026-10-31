<!--
  An action the public demo refuses (ADR 0068), as the app shows it there. The services enforce
  the demo (their `DEMO_POLICY`); this only stops offering what they would answer with 403
  `demo-mode`, the same way on every page. Outside the demo it renders its content unchanged.

  - `block` (default): a plate "In der Demo nicht verfügbar" above the content, and the content
    in a disabled fieldset: every button, field and switch inside is disabled, text and lists stay
    readable, so the visitor still sees what MowDeck offers.
  - `inline`: the same for one control in a row, with a small tag beside it instead of the plate.
  - `hide`: nothing at all, for entry points (links into a flow the demo refuses as a whole).

  Links inside a block are not disabled by the fieldset; lock them with `hide`.
-->
<script lang="ts">
  import type { Snippet } from 'svelte';

  import { m } from '#lib/paraglide/messages.js';

  import { demo } from './state.svelte.js';

  interface Props {
    children: Snippet;
    variant?: 'block' | 'inline' | 'hide';
    /** The plate keeps the page gutter (a block around whole panels, not inside one). */
    inset?: boolean;
    testid?: string;
  }
  let { children, variant = 'block', inset = false, testid = 'demo-lock' }: Props = $props();

  const noteId = $props.id();
</script>

{#if !demo.active}
  {@render children()}
{:else if variant === 'block'}
  <div class="md-demo-lock" class:inset data-testid={testid}>
    <p class="plate" id={noteId}>
      <span class="tape" aria-hidden="true"></span>
      <span class="words">
        <strong>{m.demo_locked()}</strong>
        <span class="hint">{m.demo_locked_hint()}</span>
      </span>
    </p>
    <fieldset class="locked" disabled aria-describedby={noteId}>
      {@render children()}
    </fieldset>
  </div>
{:else if variant === 'inline'}
  <span class="md-demo-lock inline" data-testid={testid}>
    <fieldset class="locked" disabled aria-describedby={noteId}>
      {@render children()}
    </fieldset>
    <span class="tag" id={noteId}>{m.demo_locked()}</span>
  </span>
{/if}

<style>
  .md-demo-lock {
    --lock-tape: repeating-linear-gradient(-55deg, var(--md-hud-lit) 0 4px, transparent 4px 8px);
  }

  .md-demo-lock:not(.inline) {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
    margin-block: var(--md-space-2);
  }

  /* The plate: the demo band's hatched tape, run down the edge of the note. */
  .plate {
    display: flex;
    align-items: stretch;
    gap: var(--md-space-3);
    margin: 0;
    border: var(--md-hairline) dashed var(--md-color-line-strong);
    background: var(--md-color-surface-sunken);
  }

  .inset .plate {
    margin-inline: var(--md-gutter);
  }

  .tape {
    flex: none;
    width: 10px;
    background: var(--lock-tape);
  }

  .words {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0 var(--md-space-2);
    padding: var(--md-space-2) var(--md-space-3) var(--md-space-2) 0;
  }

  strong {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .hint {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .locked {
    min-inline-size: 0;
    margin: 0;
    padding: 0;
    border: 0;
  }

  .md-demo-lock:not(.inline) > .locked {
    opacity: 0.62;
  }

  .inline {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2);
  }

  .inline > .locked {
    display: contents;
  }

  .tag {
    padding: 2px var(--md-space-2) 2px calc(var(--md-space-2) + 6px);
    border: var(--md-hairline) dashed var(--md-color-line-strong);
    background:
      linear-gradient(var(--md-color-surface-sunken), var(--md-color-surface-sunken)) 6px 0 / 100%
        100% no-repeat,
      var(--lock-tape);
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-11);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    white-space: nowrap;
  }
</style>

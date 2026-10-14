<!--
  The command bar of a mower: the commands the caller may use and, always, the emergency stop.
  Actions the caller lacks the permission for are not passed in at all; disabled actions stay
  visible and say why. The stop cannot be hidden by any prop.
-->
<script lang="ts">
  import Icon from '../icons/Icon.svelte';
  import type { CommandAction, StopState } from '../types.js';
  import EmergencyStop from './EmergencyStop.svelte';

  interface Props {
    actions: CommandAction[];
    onaction: (id: string) => void;
    stopLabel: string;
    stopStatus?: string;
    stopState?: StopState;
    onstop: () => void;
    /** Accessible name of the toolbar. */
    label: string;
  }
  let {
    actions,
    onaction,
    stopLabel,
    stopStatus = '',
    stopState = 'idle',
    onstop,
    label,
  }: Props = $props();
</script>

<div class="md-command-bar" role="toolbar" aria-label={label}>
  {#if actions.length}
    <div class="actions">
      {#each actions as action (action.id)}
        <button
          type="button"
          class="command"
          class:primary={action.primary && action.enabled}
          class:pending={action.pending}
          data-command={action.id}
          disabled={!action.enabled || action.pending}
          aria-busy={action.pending ? 'true' : undefined}
          title={!action.enabled ? action.reason : undefined}
          onclick={() => onaction(action.id)}
        >
          {#if action.icon}<Icon name={action.icon} size={18} />{/if}
          <span>{action.label}</span>
        </button>
      {/each}
    </div>
  {/if}
  <div class="stop">
    <EmergencyStop label={stopLabel} status={stopStatus} state={stopState} {onstop} />
  </div>
</div>

<style>
  .md-command-bar {
    display: grid;
    gap: var(--md-space-2);
    padding: var(--md-space-2) var(--md-gutter)
      calc(var(--md-space-2) + env(safe-area-inset-bottom, 0px));
    background: var(--md-color-surface);
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--md-space-2);
  }

  .command {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--md-space-2);
    min-height: var(--md-target-command);
    padding: 0 var(--md-space-3);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-weight: var(--md-weight-semibold);
    cursor: pointer;
    touch-action: manipulation;
    transition: background-color var(--md-motion-fast) var(--md-ease);
  }

  .command:hover:not(:disabled) {
    background: var(--md-color-surface-sunken);
  }

  .command.primary {
    border-color: var(--md-color-ink);
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .command.primary:hover:not(:disabled) {
    background: var(--md-color-ink-muted);
  }

  .command:disabled {
    border-style: dashed;
    color: var(--md-color-ink-muted);
    cursor: not-allowed;
  }

  .command.pending {
    border-style: solid;
    cursor: progress;
  }

  @media (min-width: 1024px) {
    .md-command-bar {
      padding: 0;
      border-top: 0;
      background: none;
    }
  }
</style>

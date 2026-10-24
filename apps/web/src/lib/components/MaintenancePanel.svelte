<!--
  Maintenance of the mower (ADR 0048): the actions its connection declares
  (`MowerCapabilities.maintenance`), each a row with what it does, the reason it cannot run now
  (disabled, never hidden) and the last result of this session. Every action asks first in a
  sheet that explains what will happen and what it needs; only "Ausführen" sends the command.
  A restart is confirmed with the danger button.
-->
<script lang="ts">
  import { Button, Sheet, type IconName } from '@mowdeck/ui';

  import { outcomeLabel } from '$lib/i18n';
  import {
    resultTone,
    type MaintenanceAction,
    type MaintenanceBlocker,
  } from '$lib/mower/maintenance';
  import { actionText, maintenanceBlockerText } from '$lib/mower/maintenance-labels';
  import { m } from '$lib/paraglide/messages';
  import type { CommandOutcome } from '$lib/stores/commands.svelte';

  interface Props {
    actions: MaintenanceAction[];
    blockers: Partial<Record<MaintenanceAction, MaintenanceBlocker | null>>;
    /** The newest command of each action in this session. */
    results: Partial<Record<MaintenanceAction, { status: CommandOutcome; reason: string | null }>>;
    /** A manual drive session of this browser is open (leaving the dock ends it). */
    driveSession: boolean;
    onrun: (action: MaintenanceAction) => void;
  }
  let { actions, blockers, results, driveSession, onrun }: Props = $props();

  const ICONS: Record<MaintenanceAction, IconName> = {
    leave_dock: 'dock',
    sync_map: 'layers',
    resync_rtk: 'signal',
    relocate_dock: 'pin',
    restart_mower: 'bolt',
  };

  let confirming = $state<MaintenanceAction | null>(null);
  const confirmText = $derived(confirming ? actionText(confirming) : null);
  const confirmBlocker = $derived(confirming ? (blockers[confirming] ?? null) : null);

  function resultText(status: CommandOutcome): string {
    if (status === 'accepted') return m.maint_result_sent();
    if (status === 'executed') return m.maint_result_done();
    if (status === 'pending') return m.maint_result_pending();
    return outcomeLabel(status);
  }

  function run(): void {
    const action = confirming;
    if (!action || blockers[action]) return;
    confirming = null;
    onrun(action);
  }
</script>

<div class="maintenance" data-testid="maintenance-panel">
  <p class="intro">{m.maint_intro()}</p>
  <ul class="actions">
    {#each actions as action (action)}
      {@const text = actionText(action)}
      {@const blocker = blockers[action] ?? null}
      {@const result = results[action]}
      <li class="action" data-testid="maint-{action}" data-blocked={blocker ?? 'none'}>
        <div class="text">
          <span class="name">{text.label}</span>
          <span class="short">{text.short}</span>
          {#if blocker}
            <span class="reason" data-testid="maint-{action}-reason" data-reason={blocker}>
              {maintenanceBlockerText(blocker, action)}
            </span>
          {/if}
          {#if result}
            <span
              class="result"
              data-tone={resultTone(result.status)}
              data-status={result.status}
              data-testid="maint-{action}-result"
            >
              {m.maint_result({ result: resultText(result.status) })}{result.reason
                ? ` · ${result.reason}`
                : ''}
            </span>
          {/if}
        </div>
        <Button
          icon={ICONS[action]}
          disabled={blocker !== null}
          pending={result?.status === 'pending'}
          testid="maint-{action}-open"
          onclick={() => (confirming = action)}>{text.label}</Button
        >
      </li>
    {/each}
  </ul>
</div>

<Sheet
  open={confirming !== null}
  title={confirmText ? m.maint_confirm_title({ action: confirmText.label }) : ''}
  closeLabel={m.close()}
  onclose={() => (confirming = null)}
  testid="maint-confirm"
>
  {#if confirming && confirmText}
    <p class="body" data-testid="maint-confirm-body">{confirmText.body}</p>
    {#if confirming === 'leave_dock' && driveSession}
      <p class="body warn" data-testid="maint-confirm-session">{m.maint_leave_dock_session()}</p>
    {/if}
    <p class="hint">{m.maint_confirm_requirements({ rule: confirmText.rule })}</p>
    {#if confirmBlocker}
      <p class="reason">{maintenanceBlockerText(confirmBlocker, confirming)}</p>
    {/if}
  {/if}
  {#snippet footer()}
    <div class="footer">
      <Button variant="ghost" onclick={() => (confirming = null)}>{m.cancel()}</Button>
      <Button
        variant={confirming === 'restart_mower' || confirming === 'relocate_dock'
          ? 'danger'
          : 'primary'}
        icon={confirming ? ICONS[confirming] : undefined}
        disabled={confirmBlocker !== null}
        testid="maint-confirm-go"
        onclick={run}>{m.maint_confirm_go()}</Button
      >
    </div>
  {/snippet}
</Sheet>

<style>
  .maintenance {
    display: grid;
    gap: var(--md-space-3);
  }

  .intro,
  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .actions {
    display: grid;
    margin: 0;
    padding: 0;
    list-style: none;
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .action {
    display: grid;
    gap: var(--md-space-2);
    padding: var(--md-space-3) 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .text {
    display: grid;
    gap: var(--md-space-0_5);
    min-width: 0;
  }

  .name {
    font-size: var(--md-font-size-16);
    line-height: var(--md-line-24);
    font-weight: var(--md-weight-semibold);
  }

  .short {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .reason,
  .result {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  .reason {
    color: var(--md-state-paused);
  }

  .result[data-tone='ok'] {
    color: var(--md-state-mowing);
  }

  .result[data-tone='error'] {
    color: var(--md-state-error);
  }

  .result[data-tone='pending'] {
    color: var(--md-state-charging);
  }

  .action[data-blocked='none'] .name::before {
    content: '';
    display: inline-block;
    width: 8px;
    height: 8px;
    margin-right: var(--md-space-2);
    vertical-align: middle;
    background: var(--md-state-mowing);
  }

  .body {
    margin: 0 0 var(--md-space-3);
    line-height: var(--md-line-24);
  }

  .body.warn {
    padding-left: var(--md-space-3);
    border-left: 3px solid var(--md-state-paused);
  }

  .footer {
    display: flex;
    justify-content: flex-end;
    gap: var(--md-space-2);
  }

  @media (min-width: 768px) {
    .action {
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: var(--md-space-4);
    }
  }
</style>

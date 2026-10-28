<!--
  Matter (ADR 0063): every mower the user may control is its own Matter node for Apple Home,
  Google Home or Home Assistant. Per mower: whether it is paired and with how many controllers,
  the pairing code on request (QR code and the 11-digit code, as Apple Home shows it, with copy)
  and a reset of the pairing (factory reset of the node, new code; every controller loses the
  mower). Codes are fetched only when shown and dropped again when hidden; nothing caches them.
-->
<script lang="ts">
  import { Button, Icon, ProblemNotice } from '@mowdeck/ui';

  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import { problemMessage } from '#lib/i18n.js';
  import {
    matterCalls,
    qrPath,
    type MatterApi,
    type MatterMower,
    type MatterPairing,
  } from '#lib/matter/api.js';
  import { m } from '#lib/paraglide/messages.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  interface Props {
    api?: MatterApi;
  }
  let { api = matterCalls() }: Props = $props();

  let mowers = $state<MatterMower[] | null>(null);
  let problem = $state<ApiProblem | null>(null);
  /** The mower whose code is shown, with its codes (one at a time). */
  let shown = $state<MatterPairing | null>(null);
  let busy = $state<string | null>(null);
  let confirming = $state<string | null>(null);
  let rowProblem = $state<{ id: string; problem: ApiProblem } | null>(null);

  async function load(): Promise<void> {
    try {
      mowers = await api.mowers();
      problem = null;
    } catch (error) {
      problem = asProblem(error);
    }
  }

  $effect(() => {
    void load();
  });

  function update(next: MatterMower): void {
    mowers = (mowers ?? []).map((mower) =>
      mower.mower_id === next.mower_id
        ? {
            mower_id: next.mower_id,
            name: next.name,
            commissioned: next.commissioned,
            fabric_count: next.fabric_count,
          }
        : mower,
    );
  }

  async function toggle(mower: MatterMower): Promise<void> {
    if (shown?.mower_id === mower.mower_id) {
      shown = null;
      return;
    }
    busy = mower.mower_id;
    rowProblem = null;
    try {
      shown = await api.pairing(mower.mower_id);
      update(shown);
    } catch (error) {
      rowProblem = { id: mower.mower_id, problem: asProblem(error) };
    } finally {
      busy = null;
    }
  }

  async function reset(mower: MatterMower): Promise<void> {
    if (confirming !== mower.mower_id) {
      confirming = mower.mower_id;
      return;
    }
    busy = mower.mower_id;
    rowProblem = null;
    try {
      shown = await api.reset(mower.mower_id);
      update(shown);
      confirming = null;
      toasts.push('ok', m.matter_reset_done({ name: mower.name }));
    } catch (error) {
      rowProblem = { id: mower.mower_id, problem: asProblem(error) };
    } finally {
      busy = null;
    }
  }

  async function copy(code: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(code.replaceAll('-', ''));
      toasts.push('ok', m.matter_copied());
    } catch {
      toasts.push('error', m.matter_copy_failed());
    }
  }

  function status(mower: MatterMower): string {
    return mower.commissioned
      ? m.matter_paired({ count: String(mower.fabric_count) })
      : m.matter_unpaired();
  }
</script>

<p class="pad intro">{m.matter_intro()}</p>

{#if problem && !mowers}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      detail={problem.detail}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if !mowers}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{:else if mowers.length === 0}
  <p class="pad quiet" data-testid="matter-empty">{m.matter_empty()}</p>
{:else}
  <ul class="list" data-testid="matter-mowers" aria-label={m.nav_matter()}>
    {#each mowers as mower (mower.mower_id)}
      {@const open = shown?.mower_id === mower.mower_id ? shown : null}
      <li class="row" data-testid="matter-mower">
        <div class="head">
          <span class="name">{mower.name}</span>
          <span class="state" data-paired={mower.commissioned} data-testid="matter-state">
            <span class="dot" aria-hidden="true"></span>{status(mower)}
          </span>
        </div>

        <div class="actions">
          <Button
            variant={open ? 'ghost' : 'primary'}
            icon="key"
            expanded={!!open}
            pending={busy === mower.mower_id && confirming !== mower.mower_id}
            onclick={() => toggle(mower)}
            testid="matter-show-code">{open ? m.matter_hide_code() : m.matter_show_code()}</Button
          >
          <span class="spacer"></span>
          <Button
            variant="danger"
            icon="trash"
            pending={busy === mower.mower_id && confirming === mower.mower_id}
            onclick={() => reset(mower)}
            testid={confirming === mower.mower_id ? 'matter-reset-confirm' : 'matter-reset'}
            >{confirming === mower.mower_id ? m.matter_reset_confirm() : m.matter_reset()}</Button
          >
          {#if confirming === mower.mower_id && busy !== mower.mower_id}
            <Button variant="ghost" onclick={() => (confirming = null)}>{m.cancel()}</Button>
          {/if}
        </div>
        {#if confirming === mower.mower_id}
          <p class="note">{m.matter_reset_body()}</p>
        {/if}

        {#if rowProblem?.id === mower.mower_id}
          <ProblemNotice title={problemMessage(rowProblem.problem)} />
        {/if}

        {#if open}
          {@const qr = qrPath(open.qr_matrix)}
          <div class="pairing" data-testid="matter-pairing">
            <svg
              class="qr"
              viewBox="0 0 {qr.size} {qr.size}"
              role="img"
              aria-label={m.matter_qr_label({ name: open.name })}
              shape-rendering="crispEdges"
              data-testid="matter-qr"
            >
              <!-- Black on white in both themes: scanners need the contrast. -->
              <rect width={qr.size} height={qr.size} fill="#fff" />
              <path d={qr.d} fill="#000" />
            </svg>
            <div class="code">
              <span class="label">{m.matter_manual_code()}</span>
              <span class="digits" data-testid="matter-code">{open.manual_code}</span>
              <Button variant="ghost" icon="copy" onclick={() => copy(open.manual_code)}
                >{m.matter_copy()}</Button
              >
              <ol class="steps">
                <li>{m.matter_step_add()}</li>
                <li>{m.matter_step_scan()}</li>
                <li>{m.matter_step_uncertified()}</li>
              </ol>
              {#if open.commissioned}
                <p class="note">
                  <Icon name="alert" size={16} />
                  <span>{m.matter_paired_note()}</span>
                </p>
              {/if}
            </div>
          </div>
        {/if}
      </li>
    {/each}
  </ul>
  <p class="pad quiet small">{m.matter_no_stop()}</p>
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .intro {
    max-width: 720px;
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .quiet {
    margin: 0;
    color: var(--md-color-ink-muted);
  }

  .small {
    font-size: var(--md-font-size-12);
  }

  .list {
    max-width: 720px;
    margin: 0;
    padding: 0;
    border-top: var(--md-hairline) solid var(--md-color-line);
    list-style: none;
  }

  .row {
    display: grid;
    gap: var(--md-space-3);
    padding: var(--md-space-4) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--md-space-2);
  }

  .name {
    font-weight: var(--md-weight-semibold);
  }

  .state {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-2);
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .dot {
    width: 8px;
    height: 8px;
    border: 1.5px solid currentcolor;
    border-radius: var(--md-radius-round);
  }

  .state[data-paired='true'] {
    color: var(--md-color-ink);
  }

  .state[data-paired='true'] .dot {
    background: currentcolor;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2);
  }

  .spacer {
    flex: 1;
  }

  .note {
    display: flex;
    gap: var(--md-space-2);
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .pairing {
    display: grid;
    grid-template-columns: minmax(0, 200px) minmax(0, 1fr);
    gap: var(--md-space-4);
    align-items: start;
  }

  .qr {
    width: 100%;
    max-width: 200px;
    border: var(--md-hairline) solid var(--md-color-line);
  }

  .code {
    display: grid;
    gap: var(--md-space-2);
    justify-items: start;
  }

  .label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .digits {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-20);
    font-variant-numeric: tabular-nums;
    letter-spacing: 0.06em;
  }

  .steps {
    margin: 0;
    padding-left: var(--md-space-6);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  @media (max-width: 520px) {
    .pairing {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>

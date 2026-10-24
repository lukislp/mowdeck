<script lang="ts">
  import { Button, Field, Panel, ProblemNotice, Select, TopBar } from '@mowdeck/ui';
  import { tick } from 'svelte';

  import { goto } from '$app/navigation';

  import { fleetApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import ConnectionForm from '#lib/components/ConnectionForm.svelte';
  import LinkedMowerFlow from '#lib/components/LinkedMowerFlow.svelte';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import {
    buildOptions,
    initialState,
    serverIssues,
    validate,
    type ConnectionState,
    type FieldIssue,
    type KindSchema,
  } from '#lib/connection/form.js';
  import { loadKinds } from '#lib/connection/kinds.js';
  import { kindTitle } from '#lib/connection/text.js';
  import type { components } from '#lib/generated/fleet.js';
  import { problemMessage } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';
  import { notifyPlanProblem } from '#lib/plan/problems.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  type ConnectionKind = components['schemas']['ConnectionKind'];

  let kinds = $state<KindSchema[] | null>(null);
  let kindsProblem = $state<ApiProblem | null>(null);
  let name = $state('');
  let kind = $state('simulated');
  let model = $state('');
  let serial = $state('');
  let connection = $state<ConnectionState>({ values: {}, flags: {}, secretSet: {}, choice: {} });
  // Client checks run live after the first submit; Fleet's 422 field errors stay until the next.
  let tried = $state(false);
  let serverFound = $state<Record<string, FieldIssue>>({});
  let busy = $state(false);
  let problem = $state<ApiProblem | null>(null);

  const schema = $derived(kinds?.find((k) => k.kind === kind) ?? null);
  // Kinds of linked vendor accounts (cloud) add their mowers from the account's device list.
  const linked = $derived(schema?.linked_account === true);
  const kindOptions = $derived((kinds ?? []).map((k) => ({ value: k.kind, label: kindTitle(k) })));
  const issues = $derived(
    tried && schema ? { ...serverFound, ...validate(schema, connection) } : serverFound,
  );

  $effect(() => {
    void loadKinds()
      .then((found) => {
        kinds = found;
        const first = found.find((k) => k.kind === 'simulated') ?? found[0];
        if (first) selectKind(first.kind);
      })
      .catch((error: unknown) => (kindsProblem = asProblem(error)));
  });

  function selectKind(next: string): void {
    const wasLinked = linked;
    kind = next;
    const found = kinds?.find((k) => k.kind === next);
    connection = found
      ? initialState(found, null)
      : { values: {}, flags: {}, secretSet: {}, choice: {} };
    tried = false;
    serverFound = {};
    problem = null;
    // The page swaps its form: keep the focus on the kind select.
    if (wasLinked !== (found?.linked_account === true)) {
      void tick().then(() => document.getElementById('connection_kind')?.focus());
    }
  }

  async function focusFirstIssue(): Promise<void> {
    await tick();
    document.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
  }

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!schema) return;
    problem = null;
    tried = true;
    serverFound = {};
    if (Object.keys(validate(schema, connection)).length > 0) {
      await focusFirstIssue();
      return;
    }
    busy = true;
    try {
      const created = await unwrap(
        fleetApi.POST('/api/v1/fleet/mowers', {
          body: {
            name: name.trim(),
            connection_kind: kind as ConnectionKind,
            connection_options: buildOptions(schema, connection, null),
            model: model.trim() || null,
            serial: serial.trim() || null,
          },
        }),
      );
      toasts.push('ok', m.mower_registered({ name: created.name }));
      await goto('/mowers');
    } catch (error) {
      problem = asProblem(error);
      notifyPlanProblem(problem);
      serverFound = serverIssues(schema, problem);
      await focusFirstIssue();
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>{m.mower_new_title()} · {m.app_name()}</title></svelte:head>

<TopBar title={m.mower_new_title()} backHref="/mowers" backLabel={m.back()} />

{#snippet kindSelect()}
  <Select
    id="connection_kind"
    label={m.mower_connection_kind()}
    options={kindOptions}
    bind:value={() => kind, selectKind}
    hint={problem?.fieldError('connection_kind') ?? undefined}
  />
{/snippet}

{#if linked && schema}
  <div class="form">
    <Panel label={m.mower_settings_general()}>
      <div class="fields">{@render kindSelect()}</div>
    </Panel>
    {#key schema.kind}
      <LinkedMowerFlow {schema} />
    {/key}
  </div>
{:else}
  <form class="form" onsubmit={submit} novalidate>
    <Panel label={m.mower_settings_general()}>
      <div class="fields">
        {@render kindSelect()}
        <Field
          id="name"
          label={m.mower_name()}
          required
          maxlength={100}
          bind:value={name}
          error={problem?.fieldError('name') ?? null}
        />
        <Field id="model" label={m.mower_model()} hint={m.optional()} bind:value={model} />
        <Field id="serial" label={m.mower_serial()} hint={m.optional()} mono bind:value={serial} />
      </div>
    </Panel>
    <Panel label={m.mower_connection_title()}>
      {#if kindsProblem}
        <ProblemNotice
          title={problemMessage(kindsProblem)}
          reference={kindsProblem.correlationId}
          referenceLabel={m.problem_reference()}
        />
      {:else if schema}
        <ConnectionForm {schema} bind:state={connection} {issues} />
      {:else}
        <p class="quiet" role="status"><SlowLoading /></p>
      {/if}
    </Panel>
    <div class="actions">
      {#if problem}
        <ProblemNotice
          title={problemMessage(problem)}
          detail={problem.detail}
          reference={problem.correlationId}
          referenceLabel={m.problem_reference()}
        />
      {/if}
      <Button
        type="submit"
        variant="primary"
        pending={busy}
        disabled={!name.trim() || !schema}
        testid="mower-submit">{m.mower_register_submit()}</Button
      >
    </div>
  </form>
{/if}

<style>
  .form {
    max-width: 720px;
  }

  .fields {
    display: grid;
    gap: var(--md-space-4);
  }

  .quiet {
    color: var(--md-color-ink-muted);
  }

  .actions {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--md-space-3);
    padding: var(--md-space-4) var(--md-gutter) var(--md-space-8);
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  @media (min-width: 768px) {
    .fields {
      grid-template-columns: 1fr 1fr;
    }
  }
</style>

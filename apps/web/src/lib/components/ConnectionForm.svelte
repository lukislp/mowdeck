<!--
  The options of one connection kind, generated from Fleet's schema: a plate with the kind and
  what it does, the options (alternatives as a segmented choice, switches for flags, write-only
  secrets with a "set" marker) and the rarely needed ones under "advanced". Managed options (set
  by Fleet from a linked account's device list) are read-only readouts and never sent.
-->
<script lang="ts">
  import { Field, Icon, SegmentedControl, Switch } from '@mowdeck/ui';

  import {
    groupMembers,
    isActive,
    issueFor,
    type ConnectionState,
    type FieldIssue,
    type KindSchema,
    type OptionSchema,
  } from '#lib/connection/form.js';
  import {
    groupLabel,
    issueMessage,
    kindDescription,
    kindTitle,
    optionHelp,
    optionLabel,
  } from '#lib/connection/text.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    schema: KindSchema;
    state: ConnectionState;
    issues?: Record<string, FieldIssue>;
    /** Options kept as they are (keys the schema does not describe). */
    extras?: Record<string, unknown>;
  }
  let { schema, state = $bindable(), issues = {}, extras = {} }: Props = $props();

  const managed = $derived(schema.options.filter((o) => o.managed));
  const basic = $derived(schema.options.filter((o) => !o.advanced && !o.managed));
  const advanced = $derived(schema.options.filter((o) => o.advanced && !o.managed));
  const extraEntries = $derived(Object.entries(extras));

  function firstOfGroup(option: OptionSchema, list: OptionSchema[]): boolean {
    return !!option.group && list.find((o) => o.group === option.group) === option;
  }

  function placeholder(option: OptionSchema): string | undefined {
    // Only defaults: an example in the input would read like a stored value.
    if (option.default !== null && option.default !== undefined && option.type !== 'boolean') {
      return String(option.default);
    }
    return undefined;
  }

  function error(option: OptionSchema): string | null {
    const issue = issueFor(schema, option, issues);
    return issue ? issueMessage(schema.kind, option, issue) : null;
  }

  function choose(group: string, key: string): void {
    state.choice = { ...state.choice, [group]: key };
  }
</script>

{#snippet input(option: OptionSchema)}
  {#if option.type === 'boolean'}
    <Switch
      id="opt-{option.key}"
      label={optionLabel(schema.kind, option)}
      hint={optionHelp(schema.kind, option) || undefined}
      states={{ on: m.switch_on(), off: m.switch_off() }}
      testid="opt-{option.key}"
      bind:checked={state.flags[option.key]}
    />
  {:else if option.secret}
    <div class="secret">
      {#if state.secretSet[option.key]}
        <span class="set" data-testid="secret-set-{option.key}">
          <Icon name="check" size={14} />{m.conn_secret_set()}
        </span>
      {/if}
      <Field
        id="opt-{option.key}"
        type="password"
        label={optionLabel(schema.kind, option)}
        autocomplete="new-password"
        mono
        hint={state.secretSet[option.key]
          ? m.conn_secret_keep_hint()
          : optionHelp(schema.kind, option) || m.conn_secret_hint()}
        error={error(option)}
        bind:value={state.values[option.key]}
      />
    </div>
  {:else}
    <Field
      id="opt-{option.key}"
      label={optionLabel(schema.kind, option)}
      hint={optionHelp(schema.kind, option) || undefined}
      inputmode={option.type === 'integer'
        ? 'numeric'
        : option.type === 'number'
          ? 'decimal'
          : undefined}
      placeholder={placeholder(option)}
      required={option.required}
      mono={option.key !== 'account'}
      type={option.key === 'account' ? 'email' : 'text'}
      error={error(option)}
      bind:value={state.values[option.key]}
    />
  {/if}
{/snippet}

{#snippet options(list: OptionSchema[])}
  {#each list as option (option.key)}
    {#if !option.group}
      <div class="cell" class:wide={option.type === 'boolean'}>{@render input(option)}</div>
    {:else if firstOfGroup(option, list)}
      {@const members = groupMembers(schema, option.group)}
      <fieldset class="group wide" data-testid="group-{option.group}">
        <legend>{groupLabel(schema, option.group)}</legend>
        <SegmentedControl
          label={groupLabel(schema, option.group)}
          testid="choice-{option.group}"
          value={state.choice[option.group] ?? ''}
          options={members.map((o) => ({ value: o.key, label: optionLabel(schema.kind, o) }))}
          onchange={(key) => choose(option.group ?? '', key)}
        />
        {#each members.filter((o) => isActive(o, state)) as active (active.key)}
          {@render input(active)}
        {/each}
      </fieldset>
    {/if}
  {/each}
{/snippet}

<div class="connection" data-testid="connection-form" data-kind={schema.kind}>
  <div class="plate">
    <span class="code">{schema.kind}</span>
    <span class="title">{kindTitle(schema)}</span>
    <p class="about">{kindDescription(schema)}</p>
  </div>

  {#if managed.length > 0}
    <div class="managed" data-testid="connection-managed">
      <dl>
        {#each managed as option (option.key)}
          {@const value = state.values[option.key] ?? ''}
          <div class="readout">
            <dt>{optionLabel(schema.kind, option)}</dt>
            <dd class:unset={!value} data-testid="managed-{option.key}">
              {value || m.linked_managed_empty()}
            </dd>
          </div>
        {/each}
      </dl>
      <p class="managed-hint">{m.linked_managed_hint()}</p>
    </div>
  {/if}

  {#if schema.options.length === 0}
    <p class="empty" data-testid="connection-no-options">{m.conn_no_options()}</p>
  {:else}
    {#if basic.length > 0}
      <div class="grid">{@render options(basic)}</div>
    {/if}
    {#if advanced.length > 0}
      <details class="advanced">
        <summary><Icon name="chevron" size={16} />{m.conn_advanced()}</summary>
        <div class="grid">{@render options(advanced)}</div>
      </details>
    {/if}
  {/if}

  {#if extraEntries.length > 0}
    <div class="extras">
      <span class="label">{m.conn_extras()}</span>
      <ul>
        {#each extraEntries as [key, value] (key)}
          <li><span class="key">{key}</span><span class="value">{JSON.stringify(value)}</span></li>
        {/each}
      </ul>
    </div>
  {/if}
</div>

<style>
  .connection {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-4);
  }

  .plate {
    display: grid;
    grid-template-columns: auto 1fr;
    align-items: baseline;
    gap: var(--md-space-1) var(--md-space-3);
    padding: var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line);
    border-left: 3px solid var(--md-color-ink);
    background: var(--md-color-surface-sunken);
  }

  .code {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .title {
    font-weight: var(--md-weight-semibold);
  }

  .about {
    grid-column: 1 / -1;
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .managed {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
  }

  dl {
    display: grid;
    gap: var(--md-hairline);
    margin: 0;
    border: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-line);
  }

  .readout {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    min-width: 0;
    padding: var(--md-space-2) var(--md-space-3);
    background: var(--md-color-surface);
  }

  dt {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  dd {
    margin: 0;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-14);
    overflow-wrap: anywhere;
  }

  dd.unset {
    color: var(--md-color-ink-muted);
    font-family: inherit;
  }

  .managed-hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .empty {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .grid {
    display: grid;
    gap: var(--md-space-4);
  }

  .cell,
  .group {
    min-width: 0;
  }

  .group {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-3);
    margin: 0;
    padding: var(--md-space-3) 0 0;
    border: 0;
    border-top: var(--md-hairline) dashed var(--md-color-line);
  }

  legend {
    padding: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .secret {
    position: relative;
  }

  .set {
    position: absolute;
    top: 0;
    right: 0;
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    padding: 0 var(--md-space-1);
    border: var(--md-hairline) solid var(--md-color-ink);
    border-radius: var(--md-radius-1);
    color: var(--md-color-ink);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-11);
    line-height: var(--md-line-16);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .advanced {
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  summary {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    min-height: var(--md-target);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    cursor: pointer;
    list-style: none;
  }

  summary::-webkit-details-marker {
    display: none;
  }

  summary :global(svg) {
    transition: transform var(--md-motion-fast) var(--md-ease);
  }

  details[open] summary :global(svg) {
    transform: rotate(90deg);
  }

  .extras {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
  }

  .label {
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
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
  }

  li {
    display: flex;
    gap: var(--md-space-2);
  }

  .key {
    color: var(--md-color-ink-muted);
  }

  @media (min-width: 768px) {
    .grid,
    dl {
      grid-template-columns: 1fr 1fr;
    }

    .wide {
      grid-column: 1 / -1;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    summary :global(svg) {
      transition: none;
    }
  }
</style>

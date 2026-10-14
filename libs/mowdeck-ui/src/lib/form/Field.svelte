<!-- A labelled text input with hint and error (field errors from problem+json map here). -->
<script lang="ts">
  interface Props {
    id: string;
    label: string;
    value?: string;
    type?: 'text' | 'password' | 'email' | 'search' | 'time' | 'date' | 'url' | 'number';
    inputmode?: 'text' | 'numeric' | 'decimal' | 'email' | 'url';
    placeholder?: string;
    disabled?: boolean;
    testid?: string;
    autocomplete?: AutoFill;
    required?: boolean;
    hint?: string;
    error?: string | null;
    mono?: boolean;
    minlength?: number;
    maxlength?: number;
  }
  let {
    id,
    label,
    value = $bindable(''),
    type = 'text',
    autocomplete,
    required = false,
    hint,
    error = null,
    mono = false,
    minlength,
    maxlength,
    inputmode,
    placeholder,
    disabled = false,
    testid,
  }: Props = $props();
</script>

<div class="md-field" class:invalid={!!error}>
  <label for={id}>{label}</label>
  <input
    {id}
    name={id}
    {type}
    {autocomplete}
    {required}
    {minlength}
    {maxlength}
    {inputmode}
    {placeholder}
    {disabled}
    data-testid={testid}
    spellcheck={mono ? 'false' : undefined}
    class:mono
    aria-invalid={error ? 'true' : undefined}
    aria-describedby={error ? `${id}-error` : hint ? `${id}-hint` : undefined}
    bind:value
  />
  {#if error}
    <span class="error" id="{id}-error">{error}</span>
  {:else if hint}
    <span class="hint" id="{id}-hint">{hint}</span>
  {/if}
</div>

<style>
  .md-field {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
  }

  label {
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    color: var(--md-color-ink-muted);
  }

  input {
    min-height: var(--md-target);
    padding: 0 var(--md-space-3);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
  }

  input.mono {
    font-family: var(--md-font-mono);
  }

  input:disabled {
    border-style: dashed;
    background: var(--md-color-surface-sunken);
    color: var(--md-color-ink-muted);
  }

  input::placeholder {
    color: var(--md-color-ink-muted);
  }

  input:focus-visible {
    border-color: var(--md-color-focus);
  }

  .invalid input {
    border-color: var(--md-state-error);
  }

  .hint {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .error {
    color: var(--md-state-error);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }
</style>

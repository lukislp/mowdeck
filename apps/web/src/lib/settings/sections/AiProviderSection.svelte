<!--
  AI provider ("KI-Anbieter"): the user's own vision model for AI assist. A family picker
  pre-fills the model string and says which key it needs; base URL for local and compatible
  servers; the API key is write-only (never shown, "stored" when set, removable); enable switch,
  save, delete and a test call with latency, tokens and the estimated cost. Notes on costs per
  analysis and that images leave the installation with a cloud model. Below, the user's AI assist
  preference "Stereo frames when available" (default on, saved at once): with both front
  cameras live the assist sends both frames of the same moment.
  Then the opt-in to automatic AI analysis on errors (ADR 0035), saved at once, with the
  installation's limits.
-->
<script lang="ts">
  import { Button, Field, Panel, ProblemNotice, Switch } from '@mowdeck/ui';

  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import DemoLock from '#lib/demo/DemoLock.svelte';
  import { formatDateTime, formatNumber } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';
  import { toasts } from '#lib/stores/ui.svelte.js';
  import {
    visionCalls,
    type VisionPreferences,
    type ProviderCatalogue,
    type ProviderFamily,
    type ProviderSettings,
    type ProviderTest,
  } from '#lib/vision/api.js';
  import { autoAvailability, canToggle, limitsText } from '#lib/vision/auto.js';
  import { formatCost } from '#lib/vision/labels.js';
  import { visionProblemText } from '#lib/vision/problems.js';
  import {
    chooseFamily,
    findFamily,
    initialForm,
    keyMissing,
    providerBody,
    validateForm,
    type ProviderForm,
  } from '#lib/vision/provider.js';

  const api = visionCalls();

  let catalogue = $state<ProviderCatalogue | null>(null);
  let settings = $state<ProviderSettings | null>(null);
  let form = $state<ProviderForm>(initialForm(null, []));
  let loadProblem = $state<ApiProblem | null>(null);
  let problem = $state<ApiProblem | null>(null);
  let busy = $state<'save' | 'delete' | 'test' | null>(null);
  let test = $state<ProviderTest | null>(null);
  let submitted = $state(false);
  /** The stereo preference (null: not loaded yet). */
  let stereo = $state<boolean | null>(null);
  let savingStereo = $state(false);

  async function loadPreferences(): Promise<void> {
    try {
      stereo = (await api.preferences()).stereo_frames;
    } catch {
      stereo = true; // the default; saving reports a problem if the service is unreachable
    }
  }

  async function saveStereo(checked: boolean): Promise<void> {
    const previous = stereo;
    stereo = checked;
    savingStereo = true;
    try {
      stereo = (await api.savePreferences({ stereo_frames: checked })).stereo_frames;
      toasts.push('ok', m.ai_provider_saved());
    } catch (error) {
      stereo = previous;
      problem = asProblem(error);
    } finally {
      savingStereo = false;
    }
  }

  let auto = $state<VisionPreferences | null>(null);
  let autoBusy = $state(false);
  let autoProblem = $state<ApiProblem | null>(null);

  async function load(): Promise<void> {
    loadProblem = null;
    try {
      const [families, mine] = await Promise.all([api.providers(), api.provider()]);
      catalogue = families;
      settings = mine;
      form = initialForm(mine, families.families);
      auto = await api.preferences().catch(() => null);
    } catch (error) {
      loadProblem = asProblem(error);
    }
  }

  $effect(() => {
    void load();
    void loadPreferences();
  });

  const families = $derived(catalogue?.families ?? []);
  const family = $derived(findFamily(form.family, families));
  const errors = $derived(validateForm(form, family));
  const missingKey = $derived(keyMissing(form, family, settings));
  const secrets = $derived(catalogue?.secrets_available ?? settings?.secrets_available ?? true);
  const lastTest = $derived(
    test
      ? { ...test, at: null as string | null }
      : settings?.last_test
        ? { ...settings.last_test, model: settings.model ?? '' }
        : null,
  );

  function pick(next: ProviderFamily): void {
    form = chooseFamily(form, next);
    submitted = false;
  }

  function fieldError(field: string): string | null {
    return problem?.fieldError(field) ?? null;
  }

  async function save(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    submitted = true;
    if (Object.keys(errors).length) return;
    busy = 'save';
    problem = null;
    try {
      settings = await api.saveProvider(providerBody(form, family));
      form = { ...form, apiKey: '', removeKey: false };
      test = null;
      submitted = false;
      toasts.push('ok', m.ai_provider_saved());
    } catch (error) {
      problem = asProblem(error);
    } finally {
      busy = null;
    }
  }

  async function remove(): Promise<void> {
    busy = 'delete';
    problem = null;
    try {
      await api.deleteProvider();
      test = null;
      await load();
      toasts.push('ok', m.ai_provider_deleted());
    } catch (error) {
      problem = asProblem(error);
    } finally {
      busy = null;
    }
  }

  async function runTest(): Promise<void> {
    busy = 'test';
    problem = null;
    try {
      test = await api.testProvider();
    } catch (error) {
      problem = asProblem(error);
      test = null;
    } finally {
      busy = null;
    }
  }

  async function toggleAuto(enabled: boolean): Promise<void> {
    if (!auto) return;
    const previous = auto;
    auto = { ...auto, auto_analysis: enabled };
    autoBusy = true;
    autoProblem = null;
    try {
      auto = await api.savePreferences({ auto_analysis: enabled });
      toasts.push('ok', enabled ? m.ai_auto_on() : m.ai_auto_off());
    } catch (error) {
      auto = previous;
      autoProblem = asProblem(error);
    } finally {
      autoBusy = false;
    }
  }

  const availability = $derived(auto ? autoAvailability(auto, settings) : null);
  const problemText = $derived(problem ? visionProblemText(problem) : null);
  const autoProblemText = $derived(autoProblem ? visionProblemText(autoProblem) : null);
</script>

{#if loadProblem}
  <div class="pad">
    <ProblemNotice
      title={loadProblem.status === 404
        ? m.ai_provider_unavailable()
        : visionProblemText(loadProblem).title}
      detail={loadProblem.status === 404 ? null : loadProblem.detail}
      reference={loadProblem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if !catalogue || !settings}
  <p class="pad quiet" role="status">{m.loading()}</p>
{:else}
  <div class="layout">
    <p class="intro">{m.ai_provider_intro()}</p>

    <div class="status" data-testid="ai-provider-effective" data-effective={settings.effective}>
      {#if settings.effective === 'own'}
        <span>{m.vision_provider_own()}</span> <span class="mono">{settings.model}</span>
      {:else if settings.effective === 'hosted'}
        <span>{m.vision_provider_hosted()}</span>
        <span class="mono">{settings.hosted_model}</span>
      {:else}
        <span>{m.vision_provider_none()}</span>
      {/if}
    </div>

    {#if catalogue.hosted_available && catalogue.hosted_model}
      <p class="note" data-testid="ai-provider-hosted">
        {m.ai_provider_hosted_hint({ model: catalogue.hosted_model })}
      </p>
    {/if}

    {#if !secrets}
      <ProblemNotice
        tone="info"
        title={m.vision_problem_secrets()}
        detail={m.ai_provider_no_secrets()}
      />
    {/if}

    {#if problemText && problem}
      <ProblemNotice
        title={problemText.title}
        detail={problemText.detail}
        reference={problem.correlationId}
        referenceLabel={m.problem_reference()}
      />
    {/if}

    <!-- The demo uses its built-in model: Vision refuses an own provider (it would call out). -->
    <DemoLock testid="demo-lock-ai-provider">
      <form class="form" onsubmit={save} novalidate>
        <fieldset class="families">
          <legend class="label">{m.ai_provider_family()}</legend>
          {#each families as option (option.key)}
            <label class="family" class:chosen={form.family === option.key}>
              <input
                type="radio"
                name="family"
                value={option.key}
                checked={form.family === option.key}
                onchange={() => pick(option)}
              />
              <span class="family-name">{option.label}</span>
              <span class="family-meta mono">
                {option.local ? m.ai_provider_local() : m.ai_provider_cloud()}
                {#if option.needs_key && option.key_env}· {option.key_env}{/if}
              </span>
            </label>
          {/each}
        </fieldset>

        {#if family}
          <p class="note" data-testid="ai-provider-key-need">
            {#if family.needs_key}
              {m.ai_provider_needs_key({ env: family.key_env ?? family.label })}
            {:else}
              {m.ai_provider_no_key()}
            {/if}
          </p>
        {/if}

        <Field
          id="ai-model"
          label={m.ai_provider_model()}
          bind:value={form.model}
          mono
          required
          autocomplete="off"
          hint={family?.examples.length
            ? m.ai_provider_model_hint({ examples: family.examples.join(', ') })
            : undefined}
          error={(submitted ? errors.model : null) ?? fieldError('model')}
          testid="ai-model"
        />

        {#if family?.needs_api_base || form.apiBase}
          <Field
            id="ai-base"
            label={m.ai_provider_base()}
            type="url"
            inputmode="url"
            bind:value={form.apiBase}
            mono
            required={family?.needs_api_base}
            autocomplete="off"
            placeholder={family?.default_api_base ?? undefined}
            hint={m.ai_provider_base_hint()}
            error={(submitted ? errors.apiBase : null) ?? fieldError('api_base')}
            testid="ai-base"
          />
        {/if}

        {#if family?.needs_key !== false || settings.api_key_set}
          <div class="key">
            <Field
              id="ai-key"
              label={m.ai_provider_key()}
              type="password"
              bind:value={form.apiKey}
              autocomplete="new-password"
              disabled={!secrets || form.removeKey}
              placeholder={settings.api_key_set && !form.removeKey
                ? m.ai_provider_key_stored()
                : undefined}
              hint={settings.api_key_set ? m.ai_provider_key_keep() : m.ai_provider_key_hint()}
              error={fieldError('api_key')}
              testid="ai-key"
            />
            {#if settings.api_key_set}
              <Button
                variant="ghost"
                icon={form.removeKey ? 'back' : 'trash'}
                onclick={() => (form = { ...form, removeKey: !form.removeKey, apiKey: '' })}
                testid="ai-key-remove"
                >{form.removeKey
                  ? m.ai_provider_key_keep_action()
                  : m.ai_provider_key_remove()}</Button
              >
            {/if}
            {#if missingKey}
              <p class="note">{m.ai_provider_key_missing()}</p>
            {/if}
          </div>
        {/if}

        <Switch
          id="ai-enabled"
          label={m.ai_provider_enabled()}
          hint={m.ai_provider_enabled_hint()}
          states={{ on: m.switch_on(), off: m.switch_off() }}
          bind:checked={form.enabled}
          testid="ai-enabled"
        />

        <div class="notes">
          <p class="note">{m.ai_provider_cost_note()}</p>
          {#if family && !family.local}
            <p class="note warn" data-testid="ai-provider-cloud-note">
              {m.ai_provider_cloud_note()}
            </p>
          {:else if family}
            <p class="note">{m.ai_provider_local_note()}</p>
          {/if}
        </div>

        <div class="row">
          <Button
            type="submit"
            variant="primary"
            icon="check"
            pending={busy === 'save'}
            disabled={busy !== null}
            testid="ai-save">{m.ai_provider_save()}</Button
          >
          <Button
            icon="pulse"
            pending={busy === 'test'}
            disabled={busy !== null || !settings.configured}
            onclick={runTest}
            testid="ai-test">{m.ai_provider_test()}</Button
          >
          {#if settings.configured}
            <Button
              variant="danger"
              icon="trash"
              pending={busy === 'delete'}
              disabled={busy !== null}
              onclick={remove}
              testid="ai-delete">{m.ai_provider_delete()}</Button
            >
          {/if}
        </div>
      </form>
    </DemoLock>

    <Panel label={m.ai_provider_preferences()} testid="ai-preferences">
      <Switch
        id="ai-stereo"
        label={m.ai_provider_stereo()}
        hint={m.ai_provider_stereo_hint()}
        states={{ on: m.switch_on(), off: m.switch_off() }}
        checked={stereo ?? true}
        disabled={stereo === null || savingStereo}
        onchange={(checked) => void saveStereo(checked)}
        testid="ai-stereo"
      />
    </Panel>

    {#if auto && availability}
      <section class="auto" data-testid="ai-auto" data-availability={availability}>
        <Switch
          id="ai-auto"
          label={m.ai_auto_title()}
          hint={m.ai_auto_hint()}
          states={{ on: m.switch_on(), off: m.switch_off() }}
          checked={auto.auto_analysis}
          disabled={autoBusy || !canToggle(auto, availability)}
          onchange={toggleAuto}
          testid="ai-auto-switch"
        />
        <p class="note mono" data-testid="ai-auto-limits">{limitsText(auto)}</p>
        {#if availability === 'unavailable'}
          <p class="note warn">{m.ai_auto_unavailable()}</p>
        {:else if availability === 'no-provider'}
          <p class="note warn" data-testid="ai-auto-needs-provider">
            {m.ai_auto_needs_provider()}
          </p>
        {/if}
        {#if auto.auto_analysis}
          <p class="note">
            {m.ai_auto_notify_hint()}
            <a href="/settings#notifications">{m.ai_auto_notify_link()}</a>
          </p>
        {/if}
        {#if autoProblemText && autoProblem}
          <ProblemNotice
            title={autoProblemText.title}
            detail={autoProblemText.detail}
            reference={autoProblem.correlationId}
            referenceLabel={m.problem_reference()}
          />
        {/if}
      </section>
    {/if}

    {#if lastTest}
      <Panel label={m.ai_provider_test_result()} testid="ai-test-result">
        <div class="test" data-ok={lastTest.ok}>
          <p class="verdict" role="status" data-testid="ai-test-verdict">
            {lastTest.ok ? m.ai_provider_test_ok() : m.ai_provider_test_failed()}
          </p>
          <dl class="facts">
            <div>
              <dt>{m.ai_provider_model()}</dt>
              <dd class="mono">{lastTest.model || m.no_data()}</dd>
            </div>
            <div>
              <dt>{m.ai_provider_latency()}</dt>
              <dd class="mono">
                {lastTest.latency_ms == null
                  ? m.no_data()
                  : m.op_latency({ ms: formatNumber(lastTest.latency_ms) })}
              </dd>
            </div>
            <div>
              <dt>{m.ai_provider_tokens()}</dt>
              <dd class="mono">
                {lastTest.input_tokens == null && lastTest.output_tokens == null
                  ? m.no_data()
                  : m.ai_provider_tokens_value({
                      input: formatNumber(lastTest.input_tokens ?? 0),
                      output: formatNumber(lastTest.output_tokens ?? 0),
                    })}
              </dd>
            </div>
            <div>
              <dt>{m.ai_provider_cost()}</dt>
              <dd class="mono">{formatCost(lastTest.cost_usd) ?? m.no_data()}</dd>
            </div>
          </dl>
          {#if lastTest.detail}<p class="note">{lastTest.detail}</p>{/if}
          {#if lastTest.at}<p class="note mono">{formatDateTime(lastTest.at)}</p>{/if}
        </div>
      </Panel>
    {/if}
  </div>
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .quiet {
    color: var(--md-color-ink-muted);
  }

  .layout {
    display: grid;
    gap: var(--md-space-4);
    max-width: 720px;
    padding: var(--md-space-4) var(--md-gutter) var(--md-space-8);
  }

  .intro,
  .note {
    margin: 0;
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .note {
    color: var(--md-color-ink-muted);
  }

  .note.warn {
    color: var(--md-color-ink);
    font-weight: var(--md-weight-semibold);
  }

  .status {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
    padding: var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface);
    font-size: var(--md-font-size-14);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }

  .form {
    display: grid;
    gap: var(--md-space-4);
  }

  .label,
  dt {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .families {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: var(--md-space-2);
    margin: 0;
    padding: 0;
    border: 0;
  }

  .families legend {
    margin-bottom: var(--md-space-2);
    padding: 0;
  }

  .family {
    position: relative;
    display: grid;
    align-content: center;
    gap: var(--md-space-1);
    min-height: var(--md-target);
    padding: var(--md-space-2) var(--md-space-3);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    background: var(--md-color-surface);
    cursor: pointer;
  }

  .family.chosen {
    border-color: var(--md-color-ink);
    box-shadow: inset 0 0 0 var(--md-hairline) var(--md-color-ink);
  }

  .family input {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    margin: 0;
    opacity: 0;
    cursor: pointer;
  }

  .family:focus-within {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .family-name {
    font-weight: var(--md-weight-semibold);
  }

  .family-meta {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .key {
    display: grid;
    gap: var(--md-space-2);
    justify-items: start;
  }

  .key > :global(.md-field) {
    justify-self: stretch;
  }

  .notes {
    display: grid;
    gap: var(--md-space-2);
    padding: var(--md-space-3);
    border-inline-start: var(--md-border-control) solid var(--md-color-line-strong);
    background: var(--md-color-surface-sunken);
  }

  .auto {
    display: grid;
    gap: var(--md-space-2);
    padding: var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-inline-start: var(--md-border-control) solid var(--md-state-paused);
    background: var(--md-color-surface);
  }

  .auto[data-availability='unavailable'],
  .auto[data-availability='no-provider'] {
    border-inline-start-color: var(--md-color-line-strong);
  }

  .auto a {
    color: inherit;
    font-weight: var(--md-weight-semibold);
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .test {
    display: grid;
    gap: var(--md-space-2);
  }

  .verdict {
    margin: 0;
    font-weight: var(--md-weight-semibold);
  }

  .test[data-ok='true'] .verdict {
    color: var(--md-state-mowing);
  }

  .test[data-ok='false'] .verdict {
    color: var(--md-state-error);
  }

  .facts {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: var(--md-space-2);
    margin: 0;
  }

  .facts div {
    display: grid;
    gap: var(--md-space-1);
  }

  dd {
    margin: 0;
    font-size: var(--md-font-size-14);
  }
</style>

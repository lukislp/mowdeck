<!--
  The mower's own settings (ADR 0046), generated from its declaration (`MowerCapabilities.settings`):
  one row per setting, grouped (weather, battery, lights, driving, safety, voice, working hours),
  with the control its type asks for: a switch (`bool`), a stepper on the declared grid (`int`),
  segments or a list (`enum`), from and to with an on switch (`time_window`). Values are the ones
  the mower reports (`MowerState.settings`); a change shows at once and its command result
  decides (`DeviceSettingsFlow`): sending, applied by the mower, or not applied with the reason.
  Without the right to manage the mower, without the plan feature or while the mower is offline,
  every control is disabled and the reason stands above them.
-->
<script lang="ts">
  import { Button, SegmentedControl, Select, Stepper, Switch } from '@mowdeck/ui';

  import { formatNumber } from '$lib/i18n';
  import {
    crossesMidnight,
    groupSettings,
    valueProblem,
    type SettingSpec,
    type SettingValue,
    type SettingsBlocker,
    type TimeWindow,
  } from '$lib/mower/device-settings';
  import type { DeviceSettingsFlow, RowView } from '$lib/mower/device-settings.svelte';
  import {
    blockerText,
    groupLabel,
    optionLabel,
    settingHint,
    settingLabel,
  } from '$lib/mower/device-settings-labels';
  import { m } from '$lib/paraglide/messages';

  interface Props {
    specs: SettingSpec[];
    /** `MowerState.settings` (null without a state). */
    values: Record<string, unknown> | null;
    blocker: SettingsBlocker | null;
    flow: DeviceSettingsFlow;
    now: number;
  }
  let { specs, values, blocker, flow, now }: Props = $props();

  const sections = $derived(groupSettings(specs));
  /** Drafts of the time windows (two fields, applied together). */
  let windows = $state<Record<string, TimeWindow>>({});

  function reported(spec: SettingSpec): SettingValue | undefined {
    if (!values || !Object.hasOwn(values, spec.key)) return undefined;
    const value = values[spec.key] as SettingValue;
    return valueProblem(spec, value) === null ? value : undefined;
  }

  function rowOf(spec: SettingSpec): RowView {
    return flow.view(spec.key, reported(spec), now);
  }

  function change(spec: SettingSpec, value: SettingValue, row: RowView): void {
    if (blocker || valueProblem(spec, value) !== null) return;
    void flow.change(spec, value, row.value);
  }

  function noteOf(row: RowView): string | null {
    switch (row.state) {
      case 'sending':
        return m.devset_state_sending();
      case 'saved':
        return m.devset_state_saved();
      case 'failed':
        return row.reason
          ? m.devset_state_failed_reason({ reason: row.reason })
          : m.devset_state_failed();
      case 'unconfirmed':
        return m.devset_state_unconfirmed();
      default:
        return null;
    }
  }

  function windowDraft(spec: SettingSpec, row: RowView): TimeWindow {
    const current = row.value && typeof row.value === 'object' ? row.value : null;
    return windows[spec.key] ?? current ?? { start: '22:00', end: '07:00' };
  }

  function setWindow(key: string, part: 'start' | 'end', value: string, base: TimeWindow): void {
    windows = { ...windows, [key]: { ...base, [part]: value } };
  }

  function applyWindow(spec: SettingSpec, row: RowView, draft: TimeWindow): void {
    change(spec, { start: draft.start, end: draft.end }, row);
    windows = Object.fromEntries(Object.entries(windows).filter(([key]) => key !== spec.key));
  }
</script>

<div class="settings" data-testid="device-settings" data-blocker={blocker}>
  <p class="intro">{m.devset_intro()}</p>
  {#if blocker}
    <p class="blocker" data-testid="device-settings-blocker" data-reason={blocker}>
      {blockerText(blocker)}
    </p>
  {/if}
  <fieldset disabled={blocker !== null}>
    <legend class="md-visually-hidden">{m.devset_title()}</legend>
    {#each sections as section (section.group)}
      <section class="group" data-group={section.group}>
        <h3>{groupLabel(section.group)}</h3>
        {#each section.specs as spec (spec.key)}
          {@const row = rowOf(spec)}
          {@const note = noteOf(row)}
          {@const label = settingLabel(spec.key)}
          {@const hint = settingHint(spec.key)}
          <div
            class="row"
            data-testid="setting-{spec.key}"
            data-type={spec.type}
            data-state={row.state}
            data-reported={row.value === undefined ? 'false' : 'true'}
          >
            {#if spec.type === 'bool'}
              <Switch
                id="setting-{spec.key}-input"
                {label}
                hint={hint ?? undefined}
                checked={row.value === true}
                states={{ on: m.switch_on(), off: m.switch_off() }}
                disabled={blocker !== null || row.state === 'sending'}
                testid="setting-{spec.key}-switch"
                onchange={(on) => change(spec, on, row)}
              />
            {:else if spec.type === 'int'}
              <Stepper
                id="setting-{spec.key}-input"
                {label}
                value={typeof row.value === 'number' ? row.value : null}
                min={spec.min ?? 0}
                max={spec.max ?? 0}
                step={spec.step ?? 1}
                unit={spec.unit ?? ''}
                format={(v) => formatNumber(v)}
                decreaseLabel={m.devset_lower({ name: label })}
                increaseLabel={m.devset_raise({ name: label })}
                disabled={blocker !== null}
                pending={row.state === 'sending'}
                oncommit={(value) => change(spec, value, row)}
              />
              {#if hint}<p class="hint">{hint}</p>{/if}
            {:else if spec.type === 'enum'}
              <span class="label" id="setting-{spec.key}-label">{label}</span>
              {#if (spec.options ?? []).length <= 3}
                <SegmentedControl
                  {label}
                  options={(spec.options ?? []).map((o) => ({
                    value: o,
                    label: optionLabel(spec.key, o),
                  }))}
                  value={typeof row.value === 'string' ? row.value : ''}
                  onchange={(value) => change(spec, value, row)}
                  testid="setting-{spec.key}-choice"
                />
              {:else}
                <Select
                  id="setting-{spec.key}-input"
                  {label}
                  options={(spec.options ?? []).map((o) => ({
                    value: o,
                    label: optionLabel(spec.key, o),
                  }))}
                  bind:value={
                    () => (typeof row.value === 'string' ? row.value : ''),
                    (value) => change(spec, value, row)
                  }
                />
              {/if}
              {#if hint}<p class="hint">{hint}</p>{/if}
            {:else}
              {@const draft = windowDraft(spec, row)}
              {@const on = row.value !== null && row.value !== undefined}
              {@const problem = valueProblem(spec, draft)}
              <Switch
                id="setting-{spec.key}-input"
                {label}
                hint={hint ?? undefined}
                checked={on}
                states={{ on: m.switch_on(), off: m.switch_off() }}
                disabled={blocker !== null || row.state === 'sending'}
                testid="setting-{spec.key}-switch"
                onchange={(next) => change(spec, next ? draft : null, row)}
              />
              {#if on}
                <div class="window" data-testid="setting-{spec.key}-window">
                  <label class="time">
                    <span class="label">{m.devset_window_from()}</span>
                    <input
                      type="time"
                      value={draft.start}
                      data-testid="setting-{spec.key}-start"
                      oninput={(e) => setWindow(spec.key, 'start', e.currentTarget.value, draft)}
                    />
                  </label>
                  <label class="time">
                    <span class="label">{m.devset_window_to()}</span>
                    <input
                      type="time"
                      value={draft.end}
                      data-testid="setting-{spec.key}-end"
                      oninput={(e) => setWindow(spec.key, 'end', e.currentTarget.value, draft)}
                    />
                  </label>
                  <Button
                    disabled={problem !== null || row.state === 'sending' || !(spec.key in windows)}
                    testid="setting-{spec.key}-apply"
                    onclick={() => applyWindow(spec, row, draft)}>{m.devset_window_apply()}</Button
                  >
                </div>
                {#if problem === 'empty_window'}
                  <p class="hint warn">{m.devset_window_empty()}</p>
                {:else if problem === null && crossesMidnight(draft)}
                  <p class="hint">{m.devset_window_overnight()}</p>
                {/if}
              {/if}
            {/if}
            {#if row.value === undefined && row.state === 'idle'}
              <p class="hint dim" data-testid="setting-{spec.key}-unreported">
                {m.devset_not_reported()}
              </p>
            {/if}
            <p
              class="note"
              class:quiet={!note}
              data-tone={row.state}
              aria-live="polite"
              data-testid="setting-{spec.key}-note"
            >
              {note ?? ''}
            </p>
          </div>
        {/each}
      </section>
    {/each}
  </fieldset>
</div>

<style>
  .settings {
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

  .hint.dim {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
  }

  .hint.warn {
    color: var(--md-state-error);
  }

  .blocker {
    margin: 0;
    padding: var(--md-space-2) var(--md-space-3);
    border-left: 3px solid var(--md-state-paused);
    background: var(--md-state-paused-bg);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  fieldset {
    display: grid;
    gap: var(--md-space-4);
    min-width: 0;
    margin: 0;
    padding: 0;
    border: 0;
  }

  .group {
    display: grid;
    gap: var(--md-space-2);
  }

  h3 {
    margin: 0;
    padding-bottom: var(--md-space-1);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .row {
    display: grid;
    gap: var(--md-space-1);
    padding-left: var(--md-space-3);
    border-left: 3px solid transparent;
    transition: border-color var(--md-motion-base) var(--md-ease);
  }

  .row[data-state='sending'] {
    border-left-color: var(--md-state-charging);
  }

  .row[data-state='saved'] {
    border-left-color: var(--md-state-mowing);
  }

  .row[data-state='failed'],
  .row[data-state='unconfirmed'] {
    border-left-color: var(--md-state-error);
  }

  .label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .note {
    min-height: var(--md-line-16);
    margin: 0;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  .note.quiet {
    display: none;
  }

  .note[data-tone='sending'] {
    color: var(--md-state-charging);
  }

  .note[data-tone='saved'] {
    color: var(--md-state-mowing);
  }

  .note[data-tone='failed'],
  .note[data-tone='unconfirmed'] {
    color: var(--md-state-error);
  }

  .window {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--md-space-2) var(--md-space-3);
  }

  .time {
    display: grid;
    gap: var(--md-space-1);
  }

  input[type='time'] {
    min-height: var(--md-target);
    padding: 0 var(--md-space-3);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-16);
  }

  fieldset:disabled .row {
    opacity: 0.72;
  }
</style>

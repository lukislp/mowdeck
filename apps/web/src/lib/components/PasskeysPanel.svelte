<!--
  The signed-in user's passkeys (ADR 0050): each one as a key tag with name, provider or device,
  added and last used, synced or bound to one device; add (password again, then the device's
  own dialog), rename, remove. The password always stays, so the last passkey can go too.
  Inside the iOS/Android app passkeys cannot be created (the web view has no access to them);
  the panel explains where to add one instead.
-->
<script lang="ts">
  import { Button, Field, Icon, Panel, ProblemNotice, Sheet } from '@mowdeck/ui';

  import { identityApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import {
    deviceHint,
    deviceName,
    passkeysEnabled,
    providerName,
    registerPasskey,
  } from '#lib/auth/passkey.js';
  import { cancelled, webauthnSupported } from '#lib/auth/webauthn.js';
  import type { components } from '#lib/generated/identity.js';
  import { formatDateTime, problemMessage } from '#lib/i18n.js';
  import { nativeShell } from '#lib/native/runtime.js';
  import { m } from '#lib/paraglide/messages.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  type Passkey = components['schemas']['PasskeyOut'];

  let enabled = $state(false);
  let passkeys = $state<Passkey[] | null>(null);
  let loadProblem = $state<ApiProblem | null>(null);

  // Adding.
  let adding = $state(false);
  let password = $state('');
  let name = $state('');
  let busy = $state(false);
  let addProblem = $state<ApiProblem | null>(null);
  let addNotice = $state<string | null>(null);

  // Renaming and removing.
  let renaming = $state<Passkey | null>(null);
  let newName = $state('');
  let confirming = $state<string | null>(null);
  let working = $state(false);

  const inApp = $derived(nativeShell.active);
  const canCreate = $derived(!inApp && typeof window !== 'undefined' && webauthnSupported(window));

  async function load(): Promise<void> {
    try {
      passkeys = await unwrap(identityApi.GET('/api/v1/identity/me/passkeys'));
    } catch (error) {
      loadProblem = asProblem(error);
    }
  }

  $effect(() => {
    void passkeysEnabled().then((on) => {
      enabled = on;
      if (on) void load();
    });
  });

  function openAdd(): void {
    adding = true;
    password = '';
    name = deviceName(navigator.userAgent);
    addProblem = null;
    addNotice = null;
  }

  async function add(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    busy = true;
    addProblem = null;
    addNotice = null;
    try {
      await registerPasskey(
        {
          options: (current) =>
            unwrap(
              identityApi.POST('/api/v1/identity/me/passkeys/options', {
                body: { password: current },
              }),
            ),
          finish: (body) => unwrap(identityApi.POST('/api/v1/identity/me/passkeys', { body })),
        },
        password,
        name.trim() || null,
      );
      toasts.push('ok', m.passkeys_added());
      adding = false;
      password = '';
      await load();
    } catch (error) {
      if (cancelled(error)) addNotice = m.passkeys_cancelled();
      else addProblem = asProblem(error);
    } finally {
      busy = false;
    }
  }

  function openRename(passkey: Passkey): void {
    renaming = passkey;
    newName = passkey.name;
  }

  async function rename(): Promise<void> {
    const target = renaming;
    if (!target || !newName.trim()) return;
    working = true;
    try {
      await unwrap(
        identityApi.PATCH('/api/v1/identity/me/passkeys/{passkey_id}', {
          params: { path: { passkey_id: target.id } },
          body: { name: newName.trim() },
        }),
      );
      toasts.push('ok', m.passkeys_renamed());
      renaming = null;
      await load();
    } catch (error) {
      const found = asProblem(error);
      toasts.push('error', problemMessage(found), found.detail ?? undefined);
    } finally {
      working = false;
    }
  }

  async function remove(passkey: Passkey): Promise<void> {
    working = true;
    try {
      await unwrap(
        identityApi.DELETE('/api/v1/identity/me/passkeys/{passkey_id}', {
          params: { path: { passkey_id: passkey.id } },
        }),
      );
      toasts.push('ok', m.passkeys_removed());
      confirming = null;
      await load();
    } catch (error) {
      const found = asProblem(error);
      toasts.push('error', problemMessage(found), found.detail ?? undefined);
    } finally {
      working = false;
    }
  }

  function origin(passkey: Passkey): string | null {
    return providerName(passkey.aaguid) ?? deviceHint(passkey.user_agent);
  }
</script>

{#if enabled}
  <Panel label={m.passkeys_title()} testid="passkeys-panel">
    {#snippet actions()}
      {#if canCreate && !adding}
        <Button variant="ghost" icon="plus" testid="passkey-add" onclick={openAdd}
          >{m.passkeys_add()}</Button
        >
      {/if}
    {/snippet}
    <p class="intro">{m.passkeys_intro()}</p>

    {#if loadProblem}
      <ProblemNotice
        title={problemMessage(loadProblem)}
        reference={loadProblem.correlationId}
        referenceLabel={m.problem_reference()}
      />
    {:else if passkeys && passkeys.length === 0}
      <p class="empty" data-testid="passkeys-empty">{m.passkeys_empty()}</p>
    {:else if passkeys}
      <ul class="keys" data-testid="passkeys">
        {#each passkeys as passkey (passkey.id)}
          <li class="key" data-testid="passkey-{passkey.name}">
            <span class="glyph" aria-hidden="true"><Icon name="key" size={18} /></span>
            <span class="body">
              <span class="name">{passkey.name}</span>
              {#if origin(passkey)}<span class="origin">{origin(passkey)}</span>{/if}
              <dl class="meta">
                <div>
                  <dt>{m.passkeys_created()}</dt>
                  <dd>{formatDateTime(passkey.created_at)}</dd>
                </div>
                <div>
                  <dt>{m.passkeys_last_used()}</dt>
                  <dd>{passkey.last_used_at ? formatDateTime(passkey.last_used_at) : m.never()}</dd>
                </div>
              </dl>
              <span class="scope" data-synced={passkey.synced}>
                {passkey.synced ? m.passkeys_synced() : m.passkeys_device_bound()}
              </span>
            </span>
            <span class="tools">
              {#if confirming === passkey.id}
                <span class="confirm" role="alert">
                  <span>{m.passkeys_remove_confirm({ name: passkey.name })}</span>
                  <span class="row">
                    <Button
                      variant="danger"
                      icon="trash"
                      pending={working}
                      testid="passkey-remove-confirm"
                      onclick={() => remove(passkey)}>{m.remove()}</Button
                    >
                    <Button variant="ghost" onclick={() => (confirming = null)}>{m.cancel()}</Button
                    >
                  </span>
                </span>
              {:else}
                <Button
                  variant="ghost"
                  icon="edit"
                  testid="passkey-rename-{passkey.name}"
                  onclick={() => openRename(passkey)}>{m.passkeys_rename()}</Button
                >
                <Button
                  variant="ghost"
                  icon="trash"
                  label={m.passkeys_remove_named({ name: passkey.name })}
                  testid="passkey-remove-{passkey.name}"
                  onclick={() => (confirming = passkey.id)}>{m.remove()}</Button
                >
              {/if}
            </span>
          </li>
        {/each}
      </ul>
    {/if}

    {#if inApp}
      <p class="hint" data-testid="passkeys-in-app">{m.passkeys_in_app_hint()}</p>
    {:else if !canCreate}
      <p class="hint">{m.passkeys_unsupported()}</p>
    {:else if adding}
      <form class="add" onsubmit={add} novalidate data-testid="passkey-add-form">
        <p class="hint">{m.passkeys_add_hint()}</p>
        <Field
          id="passkey-password"
          type="password"
          label={m.password_current()}
          autocomplete="current-password"
          required
          bind:value={password}
        />
        <Field id="passkey-name" label={m.passkeys_name()} hint={m.optional()} bind:value={name} />
        {#if addProblem}
          <ProblemNotice
            title={problemMessage(addProblem)}
            detail={addProblem.detail}
            reference={addProblem.correlationId}
            referenceLabel={m.problem_reference()}
          />
        {:else if addNotice}
          <ProblemNotice tone="info" title={addNotice} />
        {/if}
        <div class="row">
          <Button
            type="submit"
            variant="primary"
            icon="key"
            pending={busy}
            disabled={!password}
            testid="passkey-create">{m.passkeys_create()}</Button
          >
          <Button variant="ghost" onclick={() => (adding = false)}>{m.cancel()}</Button>
        </div>
      </form>
    {/if}
  </Panel>
{/if}

<Sheet
  open={renaming !== null}
  title={m.passkeys_rename_title()}
  closeLabel={m.close()}
  onclose={() => (renaming = null)}
  testid="passkey-rename-sheet"
>
  <Field id="passkey-new-name" label={m.passkeys_name()} bind:value={newName} />
  {#snippet footer()}
    <Button
      variant="primary"
      full
      icon="check"
      pending={working}
      disabled={!newName.trim()}
      testid="passkey-rename-save"
      onclick={rename}>{m.save()}</Button
    >
  {/snippet}
</Sheet>

<style>
  .intro,
  .hint,
  .empty {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .intro {
    max-width: 60ch;
  }

  .keys {
    display: grid;
    gap: var(--md-space-2);
    margin: var(--md-space-3) 0;
    padding: 0;
    list-style: none;
  }

  /* A key tag: the hairline frame of the instrument panel, the glyph in its own cell. */
  .key {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: var(--md-space-3);
    padding: var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
  }

  .glyph {
    display: grid;
    place-items: center;
    width: 36px;
    height: 36px;
    border: var(--md-hairline) solid var(--md-color-line);
    border-radius: var(--md-radius-round);
    background: var(--md-color-surface-sunken);
  }

  .body {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    min-width: 0;
  }

  .name {
    font-weight: var(--md-weight-semibold);
    overflow-wrap: anywhere;
  }

  .origin {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-1) var(--md-space-4);
    margin: 0;
  }

  .meta div {
    display: flex;
    gap: var(--md-space-1);
  }

  .meta dt {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .meta dd {
    margin: 0;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
  }

  .scope {
    align-self: flex-start;
    padding: 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line);
    border-radius: var(--md-radius-1);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
  }

  .tools {
    display: flex;
    flex-wrap: wrap;
    grid-column: 1 / -1;
    gap: var(--md-space-2);
    justify-content: flex-end;
  }

  .confirm {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
    width: 100%;
    font-size: var(--md-font-size-14);
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .add {
    display: grid;
    gap: var(--md-space-4);
    max-width: 480px;
    margin-top: var(--md-space-3);
  }

  @media (min-width: 720px) {
    .key {
      grid-template-columns: auto minmax(0, 1fr) auto;
      align-items: center;
    }

    .tools {
      grid-column: auto;
    }
  }
</style>

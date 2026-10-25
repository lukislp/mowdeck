<!--
  Passkeys of a sub-user, for admins (ADR 0050): the same reach as setting the sub-user's
  password. A lost or no longer trusted passkey can be removed; the password stays.
-->
<script lang="ts">
  import { Button, Icon, ProblemNotice, Sheet } from '@mowdeck/ui';

  import { identityApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { deviceHint, providerName } from '#lib/auth/passkey.js';
  import type { components } from '#lib/generated/identity.js';
  import { formatDateTime, problemMessage } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  type Passkey = components['schemas']['PasskeyOut'];

  let {
    accountId,
    user,
    onclose,
  }: {
    accountId: string;
    user: { id: string; name: string } | null;
    onclose: () => void;
  } = $props();

  let passkeys = $state<Passkey[] | null>(null);
  let problem = $state<ApiProblem | null>(null);
  let removing = $state<string | null>(null);

  async function load(userId: string): Promise<void> {
    passkeys = null;
    problem = null;
    try {
      passkeys = await unwrap(
        identityApi.GET('/api/v1/identity/accounts/{account_id}/users/{user_id}/passkeys', {
          params: { path: { account_id: accountId, user_id: userId } },
        }),
      );
    } catch (error) {
      problem = asProblem(error);
    }
  }

  $effect(() => {
    if (user) void load(user.id);
  });

  async function remove(passkey: Passkey): Promise<void> {
    if (!user) return;
    removing = passkey.id;
    try {
      await unwrap(
        identityApi.DELETE(
          '/api/v1/identity/accounts/{account_id}/users/{user_id}/passkeys/{passkey_id}',
          { params: { path: { account_id: accountId, user_id: user.id, passkey_id: passkey.id } } },
        ),
      );
      toasts.push('ok', m.passkeys_removed());
      await load(user.id);
    } catch (error) {
      const found = asProblem(error);
      toasts.push('error', problemMessage(found), found.detail ?? undefined);
    } finally {
      removing = null;
    }
  }
</script>

<Sheet
  open={user !== null}
  title={user ? m.users_passkeys_title({ name: user.name }) : ''}
  closeLabel={m.close()}
  {onclose}
  testid="user-passkeys-sheet"
>
  <div class="content">
    <p class="hint">{m.users_passkeys_hint()}</p>
    {#if problem}
      <ProblemNotice
        title={problemMessage(problem)}
        reference={problem.correlationId}
        referenceLabel={m.problem_reference()}
      />
    {:else if passkeys && passkeys.length === 0}
      <p class="hint">{m.passkeys_empty()}</p>
    {:else if passkeys}
      <ul class="keys">
        {#each passkeys as passkey (passkey.id)}
          <li data-testid="user-passkey-{passkey.name}">
            <Icon name="key" size={16} />
            <span class="body">
              <span class="name">{passkey.name}</span>
              <span class="mono">
                {providerName(passkey.aaguid) ?? deviceHint(passkey.user_agent) ?? ''}
                {passkey.last_used_at ? formatDateTime(passkey.last_used_at) : m.never()}
              </span>
            </span>
            <Button
              variant="ghost"
              icon="trash"
              label={m.passkeys_remove_named({ name: passkey.name })}
              pending={removing === passkey.id}
              disabled={removing !== null}
              onclick={() => remove(passkey)}>{m.remove()}</Button
            >
          </li>
        {/each}
      </ul>
    {/if}
  </div>
</Sheet>

<style>
  .content {
    display: grid;
    gap: var(--md-space-3);
  }

  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .keys {
    display: grid;
    gap: var(--md-space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .keys li {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: var(--md-space-2);
    align-items: center;
    padding: var(--md-space-2) 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .body {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .name {
    font-weight: var(--md-weight-semibold);
  }

  .mono {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
  }
</style>

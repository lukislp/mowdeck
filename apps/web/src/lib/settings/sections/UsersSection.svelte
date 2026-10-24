<!--
  Users of the account and their roles. A role is assigned at a level: the whole account, a site,
  a group or a single mower (Identity's `POST /role-assignments`; staff of one site or group see
  and control only what is below it). Each assignment shows its level and can be removed.
  Identity enforces anti-escalation and that the level belongs to the account.
-->
<script lang="ts">
  import { Button, Field, Icon, Panel, ProblemNotice, Select, Sheet } from '@mowdeck/ui';

  import { identityApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { session } from '#lib/auth/session.svelte.js';
  import LevelPicker from '#lib/components/LevelPicker.svelte';
  import { levelLabel, loadPlaces, type LevelType, type Places } from '#lib/fleet/data.js';
  import type { components } from '#lib/generated/identity.js';
  import { formatDateTime, problemMessage, roleLabel } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';
  import { notifyPlanProblem } from '#lib/plan/problems.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  type Member = components['schemas']['MemberOut'];
  type Role = components['schemas']['RoleOut'];
  type Assignment = components['schemas']['RoleAssignmentOut'];

  const accountId = $derived(session.me?.account_id ?? '');
  let members = $state<Member[] | null>(null);
  let roles = $state<Role[]>([]);
  let places = $state<Places | null>(null);
  let problem = $state<ApiProblem | null>(null);
  let createProblem = $state<ApiProblem | null>(null);

  let username = $state('');
  let displayName = $state('');
  let password = $state('');
  let roleId = $state('');
  let levelType = $state<LevelType>('account');
  let levelId = $state('');
  let mustChange = $state(true);
  let busy = $state(false);

  // The assignment sheet of one member.
  let assignFor = $state<Member | null>(null);
  let assignRole = $state('');
  let assignType = $state<LevelType>('account');
  let assignId = $state('');
  let assignProblem = $state<ApiProblem | null>(null);
  let assigning = $state(false);
  let removing = $state<string | null>(null);

  const assignable = $derived(roles.filter((r) => r.kind !== 'system_owner'));
  const roleNames = $derived(new Map(roles.map((r) => [r.id, roleLabel(r.key, r.name)])));
  const roleOptions = $derived(
    assignable.map((r) => ({ value: r.id, label: roleLabel(r.key, r.name) })),
  );
  const objectNames = $derived(
    new Map<string, string>([
      ...(places?.sites ?? []).map((s) => [s.id, s.name] as [string, string]),
      ...(places?.groups ?? []).map((g) => [g.id, g.name] as [string, string]),
      ...(places?.mowers ?? []).map((x) => [x.id, x.name] as [string, string]),
    ]),
  );

  async function load(): Promise<void> {
    if (!accountId) return;
    const path = { params: { path: { account_id: accountId } } };
    try {
      const [found, foundRoles] = await Promise.all([
        unwrap(identityApi.GET('/api/v1/identity/accounts/{account_id}/users', path)),
        unwrap(identityApi.GET('/api/v1/identity/accounts/{account_id}/roles', path)),
      ]);
      members = found;
      roles = foundRoles;
      if (!roleId) roleId = foundRoles.find((r) => r.key === 'viewer')?.id ?? '';
    } catch (error) {
      problem = asProblem(error);
    }
    if (!places) places = await loadPlaces().catch(() => null);
  }

  $effect(() => {
    void load();
  });

  function level(type: LevelType, id: string): { type: LevelType; id: string } | null {
    if (type === 'account') return { type, id: accountId };
    return id ? { type, id } : null;
  }

  function levelText(assignment: Assignment): string {
    const type = assignment.level.type as LevelType;
    if (type === 'account') return levelLabel('account');
    return `${levelLabel(type)} ${objectNames.get(assignment.level.id) ?? ''}`.trim();
  }

  async function create(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    busy = true;
    createProblem = null;
    const where = level(levelType, levelId);
    try {
      const created = await unwrap(
        identityApi.POST('/api/v1/identity/accounts/{account_id}/users', {
          params: { path: { account_id: accountId } },
          body: {
            username: username.trim(),
            password,
            display_name: displayName.trim() || null,
            must_change_password: mustChange,
            assignments: roleId && where ? [{ role_id: roleId, level: where }] : [],
          },
        }),
      );
      toasts.push('ok', m.users_created({ name: created.username }));
      username = displayName = password = '';
      await load();
    } catch (error) {
      createProblem = asProblem(error);
      notifyPlanProblem(createProblem);
    } finally {
      busy = false;
    }
  }

  function openAssign(member: Member): void {
    assignFor = member;
    assignRole = roles.find((r) => r.key === 'operator')?.id ?? assignable[0]?.id ?? '';
    assignType = 'account';
    assignId = '';
    assignProblem = null;
  }

  async function assign(): Promise<void> {
    const member = assignFor;
    const where = level(assignType, assignId);
    if (!member || !assignRole || !where) return;
    assigning = true;
    assignProblem = null;
    try {
      await unwrap(
        identityApi.POST('/api/v1/identity/accounts/{account_id}/role-assignments', {
          params: { path: { account_id: accountId } },
          body: {
            role_id: assignRole,
            level: where,
            subject_type: 'user',
            subject_id: member.id,
          },
        }),
      );
      toasts.push(
        'ok',
        m.users_assigned({
          role: roleNames.get(assignRole) ?? '',
          name: member.display_name || member.username,
        }),
      );
      assignFor = null;
      await load();
    } catch (error) {
      assignProblem = asProblem(error);
    } finally {
      assigning = false;
    }
  }

  async function unassign(assignment: Assignment): Promise<void> {
    removing = assignment.id;
    try {
      await unwrap(
        identityApi.DELETE(
          '/api/v1/identity/accounts/{account_id}/role-assignments/{assignment_id}',
          { params: { path: { account_id: accountId, assignment_id: assignment.id } } },
        ),
      );
      toasts.push('ok', m.users_unassigned());
      await load();
    } catch (error) {
      const found = asProblem(error);
      toasts.push('error', problemMessage(found), found.detail ?? undefined);
    } finally {
      removing = null;
    }
  }
</script>

{#if problem}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else}
  <ul class="members" data-testid="members">
    {#each members ?? [] as member (member.id)}
      <li data-testid="member-{member.username}">
        <span class="who">
          <span class="name">{member.display_name || member.username}</span>
          <span class="mono">{member.username}</span>
        </span>
        <span class="side">
          <span class="status" data-status={member.status}>
            {member.status === 'active' ? m.users_active() : m.users_disabled()}
          </span>
          <span class="mono last">
            {member.last_login_at ? formatDateTime(member.last_login_at) : m.never()}
          </span>
        </span>
        <ul class="roles" aria-label={m.users_roles()}>
          {#if member.is_owner}<li class="tag">{m.users_owner()}</li>{/if}
          {#if member.is_admin && !member.is_owner}<li class="tag">{m.users_admin()}</li>{/if}
          {#each member.assignments ?? [] as assignment (assignment.id)}
            <li class="tag assignment" data-testid="assignment-{assignment.id}">
              <span>{roleNames.get(assignment.role_id) ?? ''}</span>
              <span class="where" data-level={assignment.level.type}>{levelText(assignment)}</span>
              {#if !member.is_owner}
                <button
                  type="button"
                  class="remove"
                  aria-label={m.users_unassign({
                    role: roleNames.get(assignment.role_id) ?? '',
                    level: levelText(assignment),
                  })}
                  disabled={removing !== null}
                  onclick={() => unassign(assignment)}
                >
                  <Icon name="close" size={14} />
                </button>
              {/if}
            </li>
          {/each}
        </ul>
        {#if !member.is_owner}
          <div class="assign">
            <Button
              variant="ghost"
              icon="plus"
              testid="assign-{member.username}"
              onclick={() => openAssign(member)}>{m.users_assign()}</Button
            >
          </div>
        {/if}
      </li>
    {/each}
  </ul>

  <Panel label={m.users_create_title()}>
    <form class="form" onsubmit={create} novalidate>
      <Field
        id="new-username"
        label={m.users_username()}
        required
        bind:value={username}
        autocomplete="off"
      />
      <Field
        id="new-display"
        label={m.users_display_name()}
        hint={m.optional()}
        bind:value={displayName}
      />
      <Field
        id="new-password"
        type="password"
        label={m.users_password()}
        hint={m.password_hint()}
        required
        autocomplete="new-password"
        bind:value={password}
      />
      <Select
        id="new-role"
        label={m.users_role()}
        options={[{ value: '', label: m.users_no_role() }, ...roleOptions]}
        bind:value={roleId}
      />
      {#if roleId}
        <LevelPicker
          id="new-level"
          {places}
          bind:type={levelType}
          bind:objectId={levelId}
          testid="new-level"
        />
      {/if}
      <label class="check">
        <input type="checkbox" bind:checked={mustChange} />
        <span>{m.users_must_change()}</span>
      </label>
      {#if createProblem}
        <ProblemNotice
          title={problemMessage(createProblem)}
          detail={createProblem.detail}
          reference={createProblem.correlationId}
          referenceLabel={m.problem_reference()}
        />
      {/if}
      <div>
        <Button
          type="submit"
          variant="primary"
          icon="plus"
          pending={busy}
          disabled={!username || !password || (!!roleId && !level(levelType, levelId))}
          testid="user-submit">{m.users_create()}</Button
        >
      </div>
    </form>
  </Panel>
{/if}

<Sheet
  open={assignFor !== null}
  title={assignFor
    ? m.users_assign_title({ name: assignFor.display_name || assignFor.username })
    : ''}
  closeLabel={m.close()}
  onclose={() => (assignFor = null)}
  testid="assign-sheet"
>
  <div class="form">
    <p class="hint">{m.users_assign_hint()}</p>
    <Select id="assign-role" label={m.users_role()} options={roleOptions} bind:value={assignRole} />
    <LevelPicker
      id="assign-level"
      {places}
      bind:type={assignType}
      bind:objectId={assignId}
      testid="assign-level"
    />
    {#if assignProblem}
      <ProblemNotice
        title={problemMessage(assignProblem)}
        detail={assignProblem.detail}
        reference={assignProblem.correlationId}
        referenceLabel={m.problem_reference()}
      />
    {/if}
  </div>
  {#snippet footer()}
    <Button
      variant="primary"
      full
      icon="check"
      pending={assigning}
      disabled={!assignRole || !level(assignType, assignId)}
      testid="assign-save"
      onclick={assign}>{m.users_assign()}</Button
    >
  {/snippet}
</Sheet>

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .members {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .members > li {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: var(--md-space-2) var(--md-space-3);
    padding: var(--md-space-3) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  .who,
  .side {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .side {
    align-items: flex-end;
  }

  .name {
    font-weight: var(--md-weight-semibold);
  }

  .mono {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
  }

  .roles {
    display: flex;
    grid-column: 1 / -1;
    flex-wrap: wrap;
    gap: var(--md-space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .tag {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-2);
    min-height: 32px;
    padding: 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .assignment {
    min-height: var(--md-target);
    padding-inline-end: 0;
  }

  .where {
    color: var(--md-color-ink-muted);
    font-weight: var(--md-weight-regular);
    letter-spacing: 0;
    text-transform: none;
  }

  .remove {
    display: grid;
    place-items: center;
    align-self: stretch;
    width: var(--md-target);
    padding: 0;
    border: 0;
    border-inline-start: var(--md-hairline) solid var(--md-color-line);
    border-radius: var(--md-radius-0);
    background: none;
    color: var(--md-color-ink);
    cursor: pointer;
  }

  .remove:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .assign {
    grid-column: 1 / -1;
  }

  .status {
    font-size: var(--md-font-size-12);
    text-transform: uppercase;
    letter-spacing: var(--md-label-tracking);
    color: var(--md-state-mowing);
  }

  .status[data-status='disabled'] {
    color: var(--md-state-error);
  }

  .last {
    text-align: end;
  }

  .form {
    display: grid;
    gap: var(--md-space-4);
    max-width: 560px;
  }

  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .check {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    min-height: var(--md-target);
  }

  .check input {
    width: 20px;
    height: 20px;
    accent-color: var(--md-color-ink);
  }
</style>

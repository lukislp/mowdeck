<script lang="ts">
  import { Button, Field, Panel, ProblemNotice, Readout, SegmentedControl } from '@mowdeck/ui';

  import { goto } from '$app/navigation';
  import { page } from '$app/state';

  import { fleetApi, identityApi, schedulerApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { session } from '#lib/auth/session.svelte.js';
  import { probeBilling, type BillingStatus } from '#lib/billing/billing.js';
  import BillingPanel from '#lib/components/BillingPanel.svelte';
  import PlanPanel from '#lib/components/PlanPanel.svelte';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import DemoLock from '#lib/demo/DemoLock.svelte';
  import { problemMessage, roleLabel } from '#lib/i18n.js';
  import { nativeShell } from '#lib/native/runtime.js';
  import { m } from '#lib/paraglide/messages.js';
  import { getLocale, setLocale } from '#lib/paraglide/runtime.js';
  import { accountPlan } from '#lib/plan/plan.svelte.js';
  import type { LimitKey } from '#lib/plan/types.js';
  import { install, theme, toasts, type ThemeChoice } from '#lib/stores/ui.svelte.js';

  const me = $derived(session.me);
  const mustChange = $derived(me?.must_change_password === true);
  const focusPassword = $derived(mustChange || page.url.searchParams.get('password') === '1');

  let current = $state('');
  let next = $state('');
  let busy = $state(false);
  let problem = $state<ApiProblem | null>(null);

  // The plan section: the current plan again (it may have changed) and the counts the app can
  // read with the user's rights (an owner sees the whole account; failures leave a count out).
  let usage = $state<Partial<Record<LimitKey, number | null>>>({});

  async function count(call: () => Promise<{ items: unknown[] } | unknown[]>) {
    try {
      const found = await call();
      return Array.isArray(found) ? found.length : found.items.length;
    } catch {
      return null;
    }
  }

  async function loadUsage(): Promise<void> {
    const accountId = session.me?.account_id;
    const canUsers = session.has('users.manage') && accountId;
    const [mowers, sites, groups, users, schedules] = await Promise.all([
      count(() => unwrap(fleetApi.GET('/api/v1/fleet/mowers'))),
      count(() =>
        unwrap(
          fleetApi.GET('/api/v1/fleet/sites', { params: { query: { include_default: true } } }),
        ),
      ),
      count(() => unwrap(fleetApi.GET('/api/v1/fleet/groups'))),
      canUsers
        ? count(() =>
            unwrap(
              identityApi.GET('/api/v1/identity/accounts/{account_id}/users', {
                params: { path: { account_id: accountId } },
              }),
            ),
          )
        : Promise.resolve(null),
      count(() => unwrap(schedulerApi.GET('/api/v1/scheduler/schedules'))),
    ]);
    usage = {
      max_mowers: mowers,
      max_sites: sites,
      max_groups: groups,
      max_users: users,
      max_schedules: schedules,
    };
  }

  $effect(() => {
    void accountPlan.load(true);
    void loadUsage();
  });

  // Billing: only for owners (and members granted `billing.manage`) and only when the probe of
  // `/api/v1/billing/status` finds the service; otherwise no billing element appears at all.
  const canBill = $derived(session.has('billing.manage'));
  let billing = $state<BillingStatus | null>(null);

  $effect(() => {
    if (!canBill) return;
    void probeBilling().then((status) => (billing = status));
  });

  // Back from Stripe Checkout (`?billing=success|cancel#plan`): a notice, the plan again (the
  // webhook may already have changed it), and the query parameter leaves the address bar.
  $effect(() => {
    const outcome = page.url.searchParams.get('billing');
    if (outcome !== 'success' && outcome !== 'cancel') return;
    if (outcome === 'success') toasts.push('ok', m.billing_success());
    else toasts.push('info', m.billing_cancelled());
    void accountPlan.load(true);
    const url = new URL(page.url.href);
    url.searchParams.delete('billing');
    void goto(url, { shallow: true, replace: true, state: page.state });
  });

  async function changePassword(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    busy = true;
    problem = null;
    try {
      await unwrap(
        identityApi.PUT('/api/v1/identity/me/password', {
          body: { current_password: current, new_password: next },
        }),
      );
      current = next = '';
      toasts.push('ok', m.password_changed());
      await session.loadMe();
    } catch (error) {
      problem = asProblem(error);
    } finally {
      busy = false;
    }
  }

  async function logout(): Promise<void> {
    // App shell: remove this device's APNs token while the session can still do so.
    await nativeShell.signingOut();
    await session.logout();
    await goto('/login', { replace: true });
  }
</script>

{#if mustChange}
  <div class="pad">
    <ProblemNotice tone="info" title={m.password_must_change()} />
  </div>
{/if}

<Panel label={m.account_profile()}>
  <div class="profile">
    <Readout
      label={m.account_signed_in_as()}
      value={me?.display_name || me?.username || null}
      size="inline"
      emptyText={m.no_data()}
    />
    <Readout
      label={m.account_roles()}
      value={me?.roles.map((r) => roleLabel(r, r)).join(', ') || null}
      size="inline"
      emptyText={m.no_data()}
    />
  </div>
</Panel>

<div id="plan">
  <Panel label={m.plan_title()} testid="plan-panel">
    {#if accountPlan.current}
      <PlanPanel plan={accountPlan.current} {usage} />
    {:else if accountPlan.failed}
      <p class="hint" data-testid="plan-unavailable">{m.plan_unavailable()}</p>
    {:else}
      <p class="hint" role="status"><SlowLoading /></p>
    {/if}
    {#if canBill && billing}
      <BillingPanel status={billing} currentPlan={accountPlan.current?.plan ?? null} />
    {/if}
  </Panel>
</div>

<Panel label={m.account_appearance()}>
  <SegmentedControl
    label={m.account_appearance()}
    testid="theme-toggle"
    value={theme.choice}
    options={[
      { value: 'system', label: m.theme_system() },
      { value: 'light', label: m.theme_light() },
      { value: 'dark', label: m.theme_dark() },
    ]}
    onchange={(value) => theme.set(value as ThemeChoice)}
  />
</Panel>

<Panel label={m.account_language()}>
  <SegmentedControl
    label={m.account_language()}
    testid="language-switch"
    value={getLocale()}
    options={[
      { value: 'de', label: m.lang_de() },
      { value: 'en', label: m.lang_en() },
    ]}
    onchange={(value) => setLocale(value as 'de' | 'en')}
  />
</Panel>

<!-- Not inside the iOS/Android app: it is installed natively, the panel has nothing to say. -->
{#if install.offer}
  <Panel label={m.account_install()} testid="install-panel">
    {#if install.offer === 'prompt'}
      <Button icon="download" onclick={() => install.prompt()}>{m.install_action()}</Button>
    {:else}
      <p class="hint">{m.install_ios_hint()}</p>
    {/if}
  </Panel>
{:else if install.standalone}
  <Panel label={m.account_install()} testid="install-panel">
    <p class="hint">{m.install_installed()}</p>
  </Panel>
{/if}

<Panel label={m.account_password()}>
  <DemoLock testid="demo-lock-password">
    <form class="form" class:focus={focusPassword} onsubmit={changePassword} novalidate>
      <Field
        id="current-password"
        type="password"
        label={m.password_current()}
        autocomplete="current-password"
        bind:value={current}
      />
      <Field
        id="account-new-password"
        type="password"
        label={m.password_new()}
        autocomplete="new-password"
        hint={m.password_hint()}
        bind:value={next}
      />
      {#if problem}
        <ProblemNotice
          title={problemMessage(problem)}
          detail={problem.detail}
          reference={problem.correlationId}
          referenceLabel={m.problem_reference()}
        />
      {/if}
      <div>
        <Button
          type="submit"
          variant={focusPassword ? 'primary' : 'secondary'}
          pending={busy}
          disabled={!current || !next}>{m.password_save()}</Button
        >
      </div>
    </form>
  </DemoLock>
</Panel>

<Panel label={m.account_logout()}>
  <Button icon="logout" onclick={logout} testid="logout">{m.account_logout()}</Button>
</Panel>

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter) 0;
  }

  .profile {
    display: grid;
    gap: var(--md-space-3);
  }

  .form {
    display: grid;
    gap: var(--md-space-4);
    max-width: 480px;
  }

  .hint {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }
</style>

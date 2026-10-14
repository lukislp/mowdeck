<!--
  On the mower pages while the account's plan is in grace or expired: only viewing and the
  emergency stop remain (Fleet refuses every other command, nothing new may be created). Links to
  the plan section of the account page. Hidden in every other case.
-->
<script lang="ts">
  import { Icon } from '@mowdeck/ui';

  import { formatDate } from '$lib/i18n';
  import { m } from '$lib/paraglide/messages';
  import { accountPlan } from '$lib/plan/plan.svelte';

  const plan = $derived(accountPlan.current);
</script>

{#if plan && accountPlan.restricted}
  <div class="banner" role="status" data-testid="plan-banner" data-status={plan.status}>
    <Icon name="alert" size={20} />
    <div class="text">
      <strong
        >{plan.status === 'grace'
          ? plan.grace_until
            ? m.plan_banner_grace_until({ date: formatDate(plan.grace_until) })
            : m.plan_grace_title()
          : m.plan_expired_title()}</strong
      >
      <span>{m.plan_restricted_body()}</span>
    </div>
    <a class="link" href="/settings#plan">{m.plan_banner_open()}</a>
  </div>
{/if}

<style>
  .banner {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-3);
    padding: var(--md-space-3) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-state-paused);
    border-inline-start: 4px solid var(--md-state-paused);
    background: var(--md-state-paused-bg);
    color: var(--md-color-ink);
  }

  .banner > :global(svg) {
    flex: none;
    color: var(--md-state-paused);
  }

  .text {
    display: flex;
    flex: 1;
    flex-direction: column;
    min-width: 200px;
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .link {
    display: inline-flex;
    align-items: center;
    min-height: var(--md-target);
    padding: 0 var(--md-space-2);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-13);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .link:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }
</style>

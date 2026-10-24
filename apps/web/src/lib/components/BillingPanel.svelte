<!--
  The billing block under the plan of the account (only when the probe found billing enabled and
  the user holds `billing.manage`): the plans on sale with their price, an Upgrade per plan while
  the account has no running subscription (Stripe Checkout), Manage subscription (the customer
  portal) once the account is a customer, the subscription's status and the invoices with links
  to the provider's pages.
-->
<script lang="ts">
  import { Button, Icon, ProblemNotice } from '@mowdeck/ui';
  import { onMount } from 'svelte';

  import { ApiProblem, asProblem } from '#lib/api/problem.js';
  import { BillingAccount } from '#lib/billing/account.svelte.js';
  import {
    formatAmount,
    formatPrice,
    hasOpenSubscription,
    invoiceStatusLabel,
    planTitle,
    safeExternalUrl,
    subscriptionStatusLabel,
    type BillingStatus,
  } from '#lib/billing/billing.js';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import { formatDate, problemMessage } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    status: BillingStatus;
    /** Key of the plan that applies now (`accountPlan.current?.plan`). */
    currentPlan: string | null;
    account?: BillingAccount;
    /** Sends the browser to the provider (tests replace it). */
    redirect?: (url: string) => void;
  }
  let {
    status,
    currentPlan,
    account = new BillingAccount(),
    redirect = (url) => window.location.assign(url),
  }: Props = $props();

  let busy = $state<string | null>(null);
  let actionProblem = $state<ApiProblem | null>(null);

  const summary = $derived(account.summary);
  const subscription = $derived(summary?.subscription ?? null);
  const canCheckout = $derived(summary !== null && !hasOpenSubscription(subscription));
  const subscriptionUntil = $derived.by(() => {
    if (!subscription) return null;
    if (subscription.status === 'trialing' && subscription.trial_end) {
      return m.billing_trial_ends({ date: formatDate(subscription.trial_end) });
    }
    if (!subscription.current_period_end || !hasOpenSubscription(subscription)) return null;
    const date = formatDate(subscription.current_period_end);
    return subscription.cancel_at_period_end
      ? m.billing_ends({ date })
      : m.billing_renews({ date });
  });

  onMount(() => {
    void account.load();
  });

  async function go(key: string, url: () => Promise<string>): Promise<void> {
    busy = key;
    actionProblem = null;
    try {
      const target = safeExternalUrl(await url());
      if (!target) throw new ApiProblem({ status: 502 });
      redirect(target);
    } catch (error) {
      actionProblem = asProblem(error);
      busy = null;
    }
  }

  const actionMessage = $derived(
    actionProblem === null
      ? null
      : actionProblem.status === 409
        ? m.billing_exists()
        : problemMessage(actionProblem),
  );

  function invoiceKey(number: string | null, issuedAt: string, index: number): string {
    return `${number ?? ''}-${issuedAt}-${index}`;
  }
</script>

<div class="billing" data-testid="billing-panel" data-provider={status.provider}>
  <h3>{m.billing_title()}</h3>

  {#if account.problem}
    <ProblemNotice
      title={m.billing_unavailable()}
      detail={problemMessage(account.problem)}
      reference={account.problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  {:else if summary}
    <p
      class="status"
      data-testid="billing-status"
      data-status={subscription?.status ?? 'none'}
      data-plan={subscription?.plan ?? ''}
    >
      {#if subscription}
        <span
          >{m.billing_status_line({ status: subscriptionStatusLabel(subscription.status) })}</span
        >
        {#if subscriptionUntil}<span class="muted">{subscriptionUntil}</span>{/if}
      {:else}
        <span>{m.billing_status_none()}</span>
      {/if}
    </p>
  {:else if account.loading}
    <p class="muted" role="status"><SlowLoading /></p>
  {/if}

  {#if status.plans.length}
    <section aria-labelledby="billing-plans-head">
      <h4 id="billing-plans-head">{m.billing_plans()}</h4>
      <ul class="offers">
        {#each status.plans as plan (plan.key)}
          {@const current = plan.key === currentPlan}
          <li class="offer" class:current data-testid="billing-plan-{plan.key}">
            <div class="what">
              <span class="name">{planTitle(plan)}</span>
              <span class="price">{formatPrice(plan.price, status.currency)}</span>
              {#if plan.trial_days > 0 && canCheckout}
                <span class="muted">{m.billing_trial_days({ days: plan.trial_days })}</span>
              {/if}
              {#if plan.description}<span class="muted">{plan.description}</span>{/if}
            </div>
            <div class="actions">
              {#if current}
                <span class="badge"><Icon name="check" size={14} />{m.billing_current()}</span>
              {/if}
              <!-- Without an open subscription every plan can be bought, also the current one
                   (e.g. the default plan's trial from Entitlements). -->
              {#if canCheckout}
                <Button
                  variant={current ? 'secondary' : 'primary'}
                  testid="billing-upgrade-{plan.key}"
                  pending={busy === plan.key}
                  disabled={busy !== null && busy !== plan.key}
                  onclick={() => go(plan.key, () => account.checkoutUrl(plan.key))}
                  >{current ? m.billing_subscribe() : m.billing_upgrade()}</Button
                >
              {/if}
            </div>
          </li>
        {/each}
      </ul>
      {#if status.automatic_tax}<p class="fine">{m.billing_tax_note()}</p>{/if}
    </section>
  {/if}

  {#if summary?.has_customer}
    <div class="portal">
      <Button
        icon="link"
        testid="billing-portal"
        pending={busy === 'portal'}
        disabled={busy !== null && busy !== 'portal'}
        onclick={() => go('portal', () => account.portalUrl())}>{m.billing_portal()}</Button
      >
      <span class="fine">{m.billing_portal_hint()}</span>
    </div>
  {/if}

  {#if actionProblem && actionMessage}
    <ProblemNotice
      title={actionMessage}
      detail={actionProblem.status === 409 ? null : actionProblem.detail}
      reference={actionProblem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  {/if}

  {#if summary}
    <section aria-labelledby="billing-invoices-head" data-testid="billing-invoices">
      <h4 id="billing-invoices-head">{m.billing_invoices()}</h4>
      {#if account.invoicesProblem}
        <ProblemNotice
          title={m.billing_invoices_unavailable()}
          detail={problemMessage(account.invoicesProblem)}
          reference={account.invoicesProblem.correlationId}
          referenceLabel={m.problem_reference()}
        />
      {:else if account.invoices.length === 0}
        <p class="muted">{m.billing_invoices_none()}</p>
      {:else}
        <ul class="invoices">
          {#each account.invoices as invoice, index (invoiceKey(invoice.number, invoice.issued_at, index))}
            {@const hosted = safeExternalUrl(invoice.hosted_url)}
            {@const pdf = safeExternalUrl(invoice.pdf_url)}
            <li class="invoice" data-status={invoice.status}>
              <span class="number">{invoice.number ?? m.billing_invoice_no_number()}</span>
              <span class="date">{formatDate(invoice.issued_at)}</span>
              <span class="amount"
                >{formatAmount(
                  invoice.status === 'paid' ? invoice.amount_paid : invoice.amount_due,
                  invoice.currency,
                )}</span
              >
              <span class="state">{invoiceStatusLabel(invoice.status)}</span>
              <span class="links">
                {#if hosted}
                  <a href={hosted} rel="noopener noreferrer" target="_blank"
                    >{m.billing_invoice_view()}</a
                  >
                {/if}
                {#if pdf}
                  <a href={pdf} rel="noopener noreferrer" target="_blank"
                    >{m.billing_invoice_pdf()}</a
                  >
                {/if}
              </span>
            </li>
          {/each}
        </ul>
      {/if}
    </section>
  {/if}
</div>

<style>
  .billing {
    display: grid;
    gap: var(--md-space-4);
    margin-top: var(--md-space-6);
    padding-top: var(--md-space-4);
    border-top: var(--md-hairline) dashed var(--md-color-line-strong);
  }

  h3,
  h4 {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .status {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-1) var(--md-space-3);
    margin: 0;
    font-size: var(--md-font-size-14);
  }

  .muted,
  .fine {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .fine {
    margin: var(--md-space-2) 0 0;
    font-size: var(--md-font-size-12);
  }

  .offers,
  .invoices {
    display: grid;
    gap: var(--md-space-2);
    margin: var(--md-space-2) 0 0;
    padding: 0;
    list-style: none;
  }

  .offer {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-3);
    padding: var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
  }

  .offer.current {
    border-inline-start: 4px solid var(--md-color-ink);
    background: var(--md-color-surface-sunken);
  }

  .what {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    min-width: 0;
  }

  .name {
    font-weight: var(--md-weight-semibold);
  }

  .price,
  .number,
  .date,
  .amount {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2);
  }

  .badge {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    min-height: 32px;
    padding: 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
    font-size: var(--md-font-size-13);
  }

  .badge > :global(svg) {
    color: var(--md-state-mowing);
  }

  .portal {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2) var(--md-space-3);
  }

  .portal .fine {
    margin: 0;
  }

  .invoice {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: var(--md-space-1) var(--md-space-3);
    padding: var(--md-space-2) 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    font-size: var(--md-font-size-14);
  }

  .amount,
  .links {
    justify-self: end;
  }

  .links {
    display: flex;
    gap: var(--md-space-3);
  }

  .links a {
    display: inline-flex;
    align-items: center;
    min-height: 32px;
  }

  @media (min-width: 720px) {
    .invoice {
      grid-template-columns: minmax(0, 2fr) minmax(0, 1.5fr) minmax(0, 1.5fr) minmax(0, 1fr) auto;
      align-items: center;
    }

    .amount,
    .links {
      justify-self: start;
    }
  }
</style>

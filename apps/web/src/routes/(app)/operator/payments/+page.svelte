<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  Operator console, payments (ADR 0083): only where Billing runs (the probe of
  `GET /billing/status`; the console shows the tab only then). Billing and Identity answer the
  operator endpoints to operator admins only, like every page of the console.
-->
<script lang="ts">
  import PaymentsView from '#lib/components/PaymentsView.svelte';
  import { probeBilling } from '#lib/billing/billing.js';
  import { PaymentsDashboard } from '#lib/operator/payments.svelte.js';
  import { m } from '#lib/paraglide/messages.js';
  import { getLocale } from '#lib/paraglide/runtime.js';

  const dashboard = new PaymentsDashboard();
  let billing = $state<boolean | null>(null);

  $effect(() => {
    void probeBilling().then((status) => {
      billing = status !== null;
      if (status) void dashboard.load();
    });
  });
</script>

<svelte:head><title>{m.op_pay_title()} · {m.app_name()}</title></svelte:head>

<PaymentsView {billing} {dashboard} locale={getLocale()} />

<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  The language switch (German and English). Paraglide stores the choice for the server's origin
  and reloads the page, so half-typed form input is not kept. Used by the profile settings and
  the footer of the sign-in and registration pages.
-->
<script lang="ts">
  import { SegmentedControl } from '@mowdeck/ui';

  import { m } from '#lib/paraglide/messages.js';
  import { getLocale, setLocale } from '#lib/paraglide/runtime.js';

  let { testid = 'language-switch' }: { testid?: string } = $props();
</script>

<SegmentedControl
  label={m.account_language()}
  {testid}
  value={getLocale()}
  options={[
    { value: 'de', label: m.lang_de() },
    { value: 'en', label: m.lang_en() },
  ]}
  onchange={(value) => setLocale(value as 'de' | 'en')}
/>

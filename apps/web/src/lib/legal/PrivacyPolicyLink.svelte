<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  The link to the MowDeck app's privacy policy in the language of the UI. Shown in the browser
  and in the native app (which renders this UI after sign-in). Opens in a new tab; the native
  shell hands off-origin links to the system browser.
-->
<script lang="ts">
  import { privacyPolicyUrl } from '#lib/legal/privacy-policy.js';
  import { m } from '#lib/paraglide/messages.js';
  import { getLocale } from '#lib/paraglide/runtime.js';

  const href = $derived(privacyPolicyUrl(getLocale()));
</script>

<div class="privacy" data-testid="privacy-policy">
  <p class="note">{m.privacy_policy_text()}</p>
  <a {href} target="_blank" rel="noopener noreferrer" data-testid="privacy-policy-link">
    {m.privacy_policy_link()}<span class="arrow" aria-hidden="true">↗</span><span class="sr">
      ({m.source_new_tab()})
    </span>
  </a>
</div>

<style>
  .privacy {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--md-space-2);
    padding: var(--md-space-4) var(--md-gutter) 0;
  }

  .note {
    max-width: 60ch;
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  a {
    position: relative;
    display: inline-flex;
    align-items: center;
    min-height: var(--md-target);
    color: inherit;
    text-decoration-color: var(--md-color-line-strong);
    text-underline-offset: 3px;
  }

  a:hover {
    text-decoration-color: currentcolor;
  }

  a:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .arrow {
    margin-inline-start: var(--md-space-1);
    font-family: var(--md-font-mono);
  }

  .sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
</style>

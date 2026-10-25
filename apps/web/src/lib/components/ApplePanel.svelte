<!--
  The signed-in user's Apple link (ADR 0052): the texts of Apple for `ExternalLinkPanel`. The
  linked account shows a neutral label (no e-mail address is requested from Apple). MowDeck keeps
  no Apple token, so unlinking cannot remove the authorization on Apple's side: the confirmation
  and the result say where the user removes it.
-->
<script lang="ts">
  import { m } from '#lib/paraglide/messages.js';

  import AppleMark from './AppleMark.svelte';
  import ExternalLinkPanel, { type PanelTexts } from './ExternalLinkPanel.svelte';

  const PLACE = '\u0000';
  const revokeHint = $derived(m.apple_revoke_hint({ place: PLACE }).split(PLACE));

  const texts: PanelTexts = {
    title: m.apple_title,
    intro: m.apple_intro,
    notLinked: m.apple_not_linked,
    link: m.apple_link,
    linkHint: m.apple_link_hint,
    linkContinue: m.apple_link_continue,
    wrongPassword: m.apple_wrong_password,
    linked: m.apple_linked,
    linkedAs: m.apple_linked_as,
    linkedAt: m.apple_linked_at,
    unlink: m.apple_unlink,
    unlinkConfirm: m.apple_unlink_confirm,
    unlinked: m.apple_unlinked,
    inAppHint: m.apple_in_app_hint,
    linkErrors: {
      cancelled: m.apple_link_cancelled,
      expired: m.apple_link_expired,
      denied: m.apple_link_denied,
      unavailable: m.apple_link_unavailable,
      taken: m.apple_link_taken,
      already: m.apple_link_already,
    },
  };
</script>

{#snippet mark()}<AppleMark size={20} />{/snippet}

{#snippet revoke(testid: string)}
  <p class="revoke" data-testid={testid}>
    {revokeHint[0]}<strong>{m.apple_revoke_place()}</strong>{revokeHint[1]}
  </p>
{/snippet}

<ExternalLinkPanel provider="apple" label="Apple" {texts} {mark} {revoke} />

<style>
  .revoke {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .revoke strong {
    color: var(--md-color-ink);
    font-weight: var(--md-weight-semibold);
  }
</style>

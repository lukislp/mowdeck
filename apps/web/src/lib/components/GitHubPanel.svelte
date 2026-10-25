<!--
  The signed-in user's GitHub link (ADR 0051): the texts of GitHub for `ExternalLinkPanel`.
  MowDeck keeps no GitHub token, so unlinking cannot remove the authorization on GitHub's side
  (ADR 0051, amendment 2026-10-25): the confirmation and the result point to GitHub's own page
  for that.
-->
<script lang="ts">
  import { m } from '#lib/paraglide/messages.js';

  import ExternalLinkPanel, { type PanelTexts } from './ExternalLinkPanel.svelte';
  import GitHubMark from './GitHubMark.svelte';

  /** Where a GitHub user removes an OAuth App's authorization (not the installation's settings). */
  const GITHUB_APPLICATIONS = 'https://github.com/settings/applications';
  const PLACE = '\u0000';
  const revokeHint = $derived(m.github_revoke_hint({ place: PLACE }).split(PLACE));

  const texts: PanelTexts = {
    title: m.github_title,
    intro: m.github_intro,
    notLinked: m.github_not_linked,
    link: m.github_link,
    linkHint: m.github_link_hint,
    linkContinue: m.github_link_continue,
    wrongPassword: m.github_wrong_password,
    linked: m.github_linked,
    linkedAs: m.github_linked_as,
    linkedAt: m.github_linked_at,
    unlink: m.github_unlink,
    unlinkConfirm: m.github_unlink_confirm,
    unlinked: m.github_unlinked,
    inAppHint: m.github_in_app_hint,
    linkErrors: {
      cancelled: m.github_link_cancelled,
      expired: m.github_link_expired,
      denied: m.github_link_denied,
      unavailable: m.github_link_unavailable,
      taken: m.github_link_taken,
      already: m.github_link_already,
    },
  };
</script>

{#snippet mark()}<GitHubMark size={20} />{/snippet}

{#snippet revoke(testid: string)}
  <p class="hint revoke" data-testid={testid}>
    {revokeHint[0]}<a href={GITHUB_APPLICATIONS} target="_blank" rel="noopener noreferrer"
      >{m.github_revoke_place()}</a
    >{revokeHint[1]}
  </p>
{/snippet}

<ExternalLinkPanel provider="github" label="GitHub" {texts} {mark} {revoke} />

<style>
  /* The note is rendered inside the generic panel but styled here. */
  .revoke {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .revoke a {
    color: var(--md-color-ink);
    font-weight: var(--md-weight-semibold);
    text-decoration: underline;
    text-underline-offset: 2px;
  }
</style>

/**
 * The external login providers a user can link in the settings (ADR 0051 GitHub, ADR 0052 Apple).
 * `LoginLinksPanel` renders whatever is listed here: a further provider (an OIDC login, say) is
 * one more entry, plus its messages and its mark. `id` is the provider key of the API
 * (`/me/external-identities/{id}/link`), of `GET /login-methods` and of the test ids.
 */
import type { Component } from 'svelte';

import type { LinkError } from '#lib/auth/oauth.js';
import { m } from '#lib/paraglide/messages.js';

import AppleMark from './AppleMark.svelte';
import GitHubMark from './GitHubMark.svelte';

/** The texts of one provider (paraglide message functions). */
export interface ProviderTexts {
  /** "Link with X": the accessible name of the connect button. */
  link: () => string;
  linkHint: () => string;
  linkContinue: () => string;
  wrongPassword: () => string;
  linked: () => string;
  linkedAt: () => string;
  unlink: () => string;
  unlinkConfirm: () => string;
  unlinked: () => string;
  linkErrors: Record<LinkError, () => string>;
}

export interface LinkProvider {
  id: string;
  /** The provider's name, on the button and in the linked row. */
  label: string;
  mark: Component<{ size?: number }>;
  /** Whether the link's `display_name` is shown ("GitHub · octocat"); Apple tells nothing. */
  showsAccountName: boolean;
  texts: ProviderTexts;
  /**
   * Where the authorization that stays at the provider is removed (MowDeck keeps no provider
   * token): the sentence with a `{place}` slot, the place, and its page when there is one.
   */
  revoke: {
    hint: (inputs: { place: string }) => string;
    place: () => string;
    href?: string;
  };
}

export const LINK_PROVIDERS: readonly LinkProvider[] = [
  {
    id: 'github',
    label: 'GitHub',
    mark: GitHubMark,
    showsAccountName: true,
    texts: {
      link: m.github_link,
      linkHint: m.github_link_hint,
      linkContinue: m.github_link_continue,
      wrongPassword: m.github_wrong_password,
      linked: m.github_linked,
      linkedAt: m.github_linked_at,
      unlink: m.github_unlink,
      unlinkConfirm: m.github_unlink_confirm,
      unlinked: m.github_unlinked,
      linkErrors: {
        cancelled: m.github_link_cancelled,
        expired: m.github_link_expired,
        denied: m.github_link_denied,
        unavailable: m.github_link_unavailable,
        taken: m.github_link_taken,
        already: m.github_link_already,
      },
    },
    revoke: {
      hint: m.github_revoke_hint,
      place: m.github_revoke_place,
      href: 'https://github.com/settings/applications',
    },
  },
  {
    id: 'apple',
    label: 'Apple',
    mark: AppleMark,
    showsAccountName: false,
    texts: {
      link: m.apple_link,
      linkHint: m.apple_link_hint,
      linkContinue: m.apple_link_continue,
      wrongPassword: m.apple_wrong_password,
      linked: m.apple_linked,
      linkedAt: m.apple_linked_at,
      unlink: m.apple_unlink,
      unlinkConfirm: m.apple_unlink_confirm,
      unlinked: m.apple_unlinked,
      linkErrors: {
        cancelled: m.apple_link_cancelled,
        expired: m.apple_link_expired,
        denied: m.apple_link_denied,
        unavailable: m.apple_link_unavailable,
        taken: m.apple_link_taken,
        already: m.apple_link_already,
      },
    },
    revoke: {
      hint: m.apple_revoke_hint,
      place: m.apple_revoke_place,
    },
  },
];

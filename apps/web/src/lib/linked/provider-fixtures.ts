/**
 * Provider descriptors for the app's unit tests (ADR 0065): the app knows no vendor, so its
 * tests use two neutral ones, `acme` (e-mail and password, tested on hardware) and `zeta`
 * (application key and secret plus a region, untested, developer account needed).
 */
import type { CredentialField, ProviderDescriptor } from './providers';

type FieldInput = Pick<CredentialField, 'key' | 'kind' | 'label'> & Partial<CredentialField>;

/** A field with Fleet's defaults (required, not the identity, at most 256 characters). */
export function field(input: FieldInput): CredentialField {
  return { required: true, identity: false, max_length: 256, choices: [], ...input };
}

export const ACME: ProviderDescriptor = {
  provider: 'acme',
  name: 'Acme Cloud',
  summary: 'Acme mowers through the Acme cloud.',
  fields: [
    field({ key: 'email', kind: 'email', label: 'E-mail address', identity: true }),
    field({ key: 'password', kind: 'secret', label: 'Password', help: 'Stored encrypted.' }),
  ],
  setup_steps: ['Create a second account.', 'Share the mowers with it.'],
  links: [{ label: 'Acme app', url: 'https://acme.test/app' }],
  needs_developer_account: false,
  tested_on_hardware: true,
};

export const ZETA: ProviderDescriptor = {
  provider: 'zeta',
  name: 'Zeta Connect',
  summary: 'Zeta mowers through the official Zeta API.',
  fields: [
    field({ key: 'app_key', kind: 'text', label: 'Application key', identity: true }),
    field({ key: 'app_secret', kind: 'secret', label: 'Application secret' }),
    field({
      key: 'region',
      kind: 'choice',
      label: 'Region',
      choices: [
        { value: 'eu', label: 'Europe' },
        { value: 'us', label: 'United States' },
      ],
    }),
    field({ key: 'site', kind: 'text', label: 'Site code', required: false, pattern: '[A-Z]{3}' }),
  ],
  setup_steps: ['Sign in to the Zeta developer portal.', 'Create an application.'],
  links: [{ label: 'Developer portal', url: 'https://developer.zeta.test' }],
  needs_developer_account: true,
  tested_on_hardware: false,
};

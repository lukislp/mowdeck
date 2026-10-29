/**
 * Cloud providers (Fleet's `GET /linked-accounts/providers`, ADR 0066): the link form of any
 * vendor is rendered from the provider's descriptor, so the app names no vendor. Pure functions:
 * the form's starting values, the checks the server does again (required, e-mail, choice,
 * length, pattern), the bodies of link and re-authentication, and where a problem belongs.
 */
import type { ApiProblem } from '#lib/api/problem.js';
import type { components } from '#lib/generated/fleet.js';
import { problemMessage } from '#lib/i18n.js';
import { m } from '#lib/paraglide/messages.js';

export type ProviderDescriptor = components['schemas']['ProviderDescriptor'];
export type CredentialField = ProviderDescriptor['fields'][number];

/** Values of a link form by field key. */
export type CredentialValues = Record<string, string>;

export function isSecret(field: CredentialField): boolean {
  return field.kind === 'secret';
}

export function identityField(provider: ProviderDescriptor): CredentialField {
  return provider.fields.find((f) => f.identity) ?? provider.fields[0]!;
}

export function secretFields(provider: ProviderDescriptor): CredentialField[] {
  return provider.fields.filter(isSecret);
}

/** Non-secret fields other than the identity (shown on a linked account, e.g. a brand). */
export function detailFields(provider: ProviderDescriptor): CredentialField[] {
  return provider.fields.filter((f) => !f.identity && !isSecret(f));
}

/** Empty values; a choice starts at its first option. */
export function initialValues(provider: ProviderDescriptor): CredentialValues {
  const values: CredentialValues = {};
  for (const field of provider.fields) {
    values[field.key] = field.kind === 'choice' ? (field.choices?.[0]?.value ?? '') : '';
  }
  return values;
}

/** Shown values trimmed, empty optional ones dropped, secrets as typed. */
export function credentialsBody(
  provider: ProviderDescriptor,
  values: Readonly<CredentialValues>,
  { onlySecrets = false }: { onlySecrets?: boolean } = {},
): CredentialValues {
  const body: CredentialValues = {};
  for (const field of provider.fields) {
    if (onlySecrets && !isSecret(field)) continue;
    const raw = values[field.key] ?? '';
    const value = isSecret(field) ? raw : raw.trim();
    if (value) body[field.key] = value;
  }
  return body;
}

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s.]{2,}$/;

/** The message of one field before sending (null: fine). Re-authentication needs no value. */
export function fieldError(
  field: CredentialField,
  value: string,
  { onlySecrets = false }: { onlySecrets?: boolean } = {},
): string | null {
  const clean = isSecret(field) ? value : value.trim();
  if (!clean) return field.required !== false && !onlySecrets ? m.conn_error_required() : null;
  if (field.max_length !== undefined && clean.length > field.max_length) {
    return m.provider_err_too_long({ max: field.max_length });
  }
  if (field.kind === 'email' && !EMAIL.test(clean)) return m.linked_err_email();
  if (field.kind === 'choice' && !(field.choices ?? []).some((c) => c.value === clean)) {
    return m.provider_err_format();
  }
  if (field.pattern && !new RegExp(`^(?:${field.pattern})$`).test(clean)) {
    return m.provider_err_format();
  }
  return null;
}

/** Every field's message (only the fields with one). */
export function formErrors(
  provider: ProviderDescriptor,
  values: Readonly<CredentialValues>,
  options: { onlySecrets?: boolean } = {},
): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const field of provider.fields) {
    if (options.onlySecrets && !isSecret(field)) continue;
    const error = fieldError(field, values[field.key] ?? '', options);
    if (error) errors[field.key] = error;
  }
  return errors;
}

/** Where a problem of linking or re-authenticating belongs. */
export interface CredentialIssue {
  /** Messages by field key; empty for a notice above the form. */
  fields: Record<string, string>;
  /** A notice above the form (null when the fields say it all). */
  notice: string | null;
  /** Fleet's English detail under a notice (503: no key, no gateway, cloud down). */
  detail: string | null;
}

const POINTER = /^#?\/credentials\/([a-z][a-z0-9_]*)$/;

export function credentialIssue(
  problem: ApiProblem,
  provider: ProviderDescriptor,
): CredentialIssue {
  const secret = secretFields(provider)[0]?.key;
  if (problem.slug === 'linked-account-login-failed') {
    const key = keyOf(problem) ?? secret;
    return key
      ? { fields: { [key]: m.linked_login_failed() }, notice: null, detail: null }
      : { fields: {}, notice: m.linked_login_failed(), detail: null };
  }
  if (problem.status === 409) {
    return {
      fields: { [identityField(provider).key]: m.linked_already_linked() },
      notice: null,
      detail: null,
    };
  }
  if (problem.status === 422) {
    const fields: Record<string, string> = {};
    for (const error of problem.errors) {
      const match = POINTER.exec(error.pointer ?? '');
      if (match) fields[match[1]!] = m.provider_err_format();
    }
    if (Object.keys(fields).length > 0) return { fields, notice: null, detail: null };
    if (problem.fieldError('provider')) {
      return { fields: {}, notice: m.provider_unavailable(), detail: null };
    }
  }
  if (problem.status === 503 || problem.status === 502) {
    return { fields: {}, notice: m.linked_unavailable(), detail: problem.detail };
  }
  return { fields: {}, notice: problemMessage(problem), detail: problem.detail };
}

function keyOf(problem: ApiProblem): string | null {
  for (const error of problem.errors) {
    const match = POINTER.exec(error.pointer ?? '');
    if (match) return match[1]!;
  }
  return null;
}

/** The providers by id (accounts of a provider no longer enabled are shown by their id). */
export function byId(providers: readonly ProviderDescriptor[]): Map<string, ProviderDescriptor> {
  return new Map(providers.map((p) => [p.provider, p]));
}

/** An input id for a field (two forms on one page stay distinct). */
export function inputId(prefix: string, field: CredentialField): string {
  return `${prefix}-${field.key.replace(/_/g, '-')}`;
}

/** The browser's autofill hint for a field. */
export function autocompleteOf(field: CredentialField): AutoFill {
  if (isSecret(field)) return 'new-password';
  return 'off';
}

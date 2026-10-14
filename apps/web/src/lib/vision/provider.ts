/**
 * The form of the AI provider settings ("KI-Anbieter"): which family a model string belongs to,
 * what choosing a family pre-fills, client-side checks and the `PUT /me/provider` body. The API
 * key is write-only: an empty input keeps the stored key, "remove key" sends an empty string.
 */
import { m } from '$lib/paraglide/messages';

import type { ProviderFamily, ProviderInput, ProviderSettings } from './api';

export interface ProviderForm {
  family: string;
  model: string;
  apiBase: string;
  /** A new key typed in; never filled from the server. */
  apiKey: string;
  /** Delete the stored key on save. */
  removeKey: boolean;
  enabled: boolean;
}

export type ProviderFormErrors = Partial<Record<'model' | 'apiBase', string>>;

/**
 * Whether a key is still missing: the family needs one, none is typed in and none is stored.
 * Only a hint, never a refusal: the operator may provide the key through `key_env`.
 */
export function keyMissing(
  form: ProviderForm,
  family: ProviderFamily | null,
  settings: ProviderSettings | null,
): boolean {
  const stored = !!settings?.api_key_set && !form.removeKey;
  return !!family?.needs_key && !form.apiKey.trim() && !stored;
}

/** The family whose `<prefix>/` starts `model`; the longest prefix wins. */
export function familyOfModel(
  model: string,
  families: readonly ProviderFamily[],
): ProviderFamily | null {
  const value = model.trim();
  let best: ProviderFamily | null = null;
  for (const family of families) {
    if (value.startsWith(`${family.prefix}/`)) {
      if (!best || family.prefix.length > best.prefix.length) best = family;
    }
  }
  return best;
}

export function findFamily(
  key: string,
  families: readonly ProviderFamily[],
): ProviderFamily | null {
  return families.find((family) => family.key === key) ?? null;
}

/** The form for the stored settings (or the first family's defaults when none are stored). */
export function initialForm(
  settings: ProviderSettings | null,
  families: readonly ProviderFamily[],
): ProviderForm {
  if (settings?.configured && settings.model) {
    const family =
      (settings.family ? findFamily(settings.family, families) : null) ??
      familyOfModel(settings.model, families);
    return {
      family: family?.key ?? '',
      model: settings.model,
      apiBase: settings.api_base ?? family?.default_api_base ?? '',
      apiKey: '',
      removeKey: false,
      enabled: settings.enabled,
    };
  }
  const first = families[0];
  const base: ProviderForm = {
    family: '',
    model: '',
    apiBase: '',
    apiKey: '',
    removeKey: false,
    enabled: true,
  };
  return first ? chooseFamily(base, first) : base;
}

/** Switch to `family`: its first example as the model and its default base URL. */
export function chooseFamily(form: ProviderForm, family: ProviderFamily): ProviderForm {
  return {
    ...form,
    family: family.key,
    model: family.examples[0] ?? `${family.prefix}/`,
    apiBase: family.needs_api_base ? (family.default_api_base ?? '') : '',
  };
}

/** Problems the server would refuse anyway, shown before saving. */
export function validateForm(
  form: ProviderForm,
  family: ProviderFamily | null,
): ProviderFormErrors {
  const errors: ProviderFormErrors = {};
  const model = form.model.trim();
  if (!model || model.endsWith('/')) errors.model = m.ai_provider_error_model();
  else if (family && !model.startsWith(`${family.prefix}/`)) {
    errors.model = m.ai_provider_error_prefix({ prefix: `${family.prefix}/` });
  }
  if (family?.needs_api_base && !form.apiBase.trim()) errors.apiBase = m.ai_provider_error_base();
  else if (form.apiBase.trim() && !/^https?:\/\/\S+$/i.test(form.apiBase.trim())) {
    errors.apiBase = m.ai_provider_error_base_url();
  }
  return errors;
}

/** The `PUT /me/provider` body: the key only when typed in (or "" to delete the stored one). */
export function providerBody(form: ProviderForm, family: ProviderFamily | null): ProviderInput {
  const body: ProviderInput = { model: form.model.trim(), enabled: form.enabled };
  const apiBase = form.apiBase.trim();
  body.api_base = family?.needs_api_base || apiBase ? apiBase || null : null;
  const key = form.apiKey.trim();
  if (key) body.api_key = key;
  else if (form.removeKey) body.api_key = '';
  return body;
}

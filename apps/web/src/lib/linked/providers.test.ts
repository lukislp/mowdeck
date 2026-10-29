import { describe, expect, it } from 'vitest';

import { ApiProblem, PROBLEM_PREFIX } from '#lib/api/problem.js';
import { m } from '#lib/paraglide/messages.js';

import { ACME, ZETA } from './provider-fixtures';
import {
  byId,
  credentialIssue,
  credentialsBody,
  detailFields,
  fieldError,
  formErrors,
  identityField,
  initialValues,
  inputId,
  secretFields,
} from './providers';

describe('provider descriptors', () => {
  it('names the identity, the secrets and the shown details', () => {
    expect(identityField(ZETA).key).toBe('app_key');
    expect(secretFields(ZETA).map((f) => f.key)).toEqual(['app_secret']);
    expect(detailFields(ZETA).map((f) => f.key)).toEqual(['region', 'site']);
    expect(byId([ACME, ZETA]).get('zeta')).toBe(ZETA);
    expect(inputId('linked', ZETA.fields[0]!)).toBe('linked-app-key');
    expect(inputId('linked', ACME.fields[0]!)).toBe('linked-email');
  });

  it('starts a choice at its first option and everything else empty', () => {
    expect(initialValues(ZETA)).toEqual({ app_key: '', app_secret: '', region: 'eu', site: '' });
  });

  it('builds the body: shown values trimmed, secrets as typed, empty optionals left out', () => {
    const values = { app_key: ' key ', app_secret: ' s3cret ', region: 'us', site: ' ' };
    expect(credentialsBody(ZETA, values)).toEqual({
      app_key: 'key',
      app_secret: ' s3cret ',
      region: 'us',
    });
    expect(credentialsBody(ZETA, values, { onlySecrets: true })).toEqual({
      app_secret: ' s3cret ',
    });
    expect(credentialsBody(ZETA, { ...values, app_secret: '' }, { onlySecrets: true })).toEqual({});
  });

  it('checks the fields like the server', () => {
    const [email, password] = ACME.fields;
    expect(fieldError(email!, '')).toBe(m.conn_error_required());
    expect(fieldError(email!, 'a@b')).toBe(m.linked_err_email());
    expect(fieldError(email!, ' garden@example.com ')).toBeNull();
    expect(fieldError(password!, 'x'.repeat(257))).toBe(m.provider_err_too_long({ max: 256 }));
    expect(fieldError(password!, '', { onlySecrets: true })).toBeNull();
    const site = ZETA.fields[3]!;
    expect(fieldError(site, '')).toBeNull(); // optional
    expect(fieldError(site, 'abc')).toBe(m.provider_err_format());
    expect(fieldError(site, 'ABC')).toBeNull();
    expect(fieldError(ZETA.fields[2]!, 'mars')).toBe(m.provider_err_format());
    expect(formErrors(ZETA, initialValues(ZETA))).toEqual({
      app_key: m.conn_error_required(),
      app_secret: m.conn_error_required(),
    });
    expect(formErrors(ZETA, initialValues(ZETA), { onlySecrets: true })).toEqual({});
  });
});

describe('credentialIssue', () => {
  it('puts a refused login on the field Fleet names, else on the first secret', () => {
    const named = new ApiProblem({
      type: `${PROBLEM_PREFIX}linked-account-login-failed`,
      status: 422,
      errors: [{ pointer: '#/credentials/app_secret', message: 'refused', code: 'login_failed' }],
    });
    expect(credentialIssue(named, ZETA).fields).toEqual({ app_secret: m.linked_login_failed() });
    const bare = new ApiProblem({
      type: `${PROBLEM_PREFIX}linked-account-login-failed`,
      status: 422,
    });
    expect(credentialIssue(bare, ACME).fields).toEqual({ password: m.linked_login_failed() });
  });

  it('puts a duplicate on the identity and field problems on their fields', () => {
    expect(credentialIssue(new ApiProblem({ status: 409 }), ZETA).fields).toEqual({
      app_key: m.linked_already_linked(),
    });
    const invalid = new ApiProblem({
      status: 422,
      errors: [{ pointer: '#/credentials/region', message: 'not one of the choices', code: 'x' }],
    });
    expect(credentialIssue(invalid, ZETA).fields).toEqual({ region: m.provider_err_format() });
  });

  it('shows a disabled provider and an unreachable cloud above the form', () => {
    const disabled = new ApiProblem({
      status: 422,
      errors: [{ pointer: '#/provider', message: 'not enabled', code: 'unknown_provider' }],
    });
    expect(credentialIssue(disabled, ZETA)).toEqual({
      fields: {},
      notice: m.provider_unavailable(),
      detail: null,
    });
    const down = credentialIssue(new ApiProblem({ status: 503, detail: 'no key' }), ZETA);
    expect(down).toEqual({ fields: {}, notice: m.linked_unavailable(), detail: 'no key' });
    const other = credentialIssue(new ApiProblem({ status: 500, detail: 'boom' }), ZETA);
    expect(other.notice).toBeTruthy();
    expect(other.fields).toEqual({});
  });
});

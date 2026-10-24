/**
 * WebAuthn in the browser (passkeys, ADR 0050): feature detection and the JSON encoding.
 *
 * Identity sends the ceremony options as JSON with base64url binary fields (the WebAuthn level 3
 * encoding) and expects `PublicKeyCredential.toJSON()` back. Browsers that implement
 * `parseCreationOptionsFromJSON` / `parseRequestOptionsFromJSON` / `toJSON` do the conversion
 * themselves; for the others (older Safari and Firefox) the small codec below does it.
 */

type Json = Record<string, unknown>;

/** base64url (no padding) of bytes. */
export function toBase64url(bytes: ArrayBuffer | ArrayBufferView): string {
  // ArrayBuffer.isView, not instanceof: buffers from another realm (crypto.subtle in tests,
  // iframes) are not instances of this realm's ArrayBuffer.
  const view = ArrayBuffer.isView(bytes)
    ? new Uint8Array(bytes.buffer, bytes.byteOffset, bytes.byteLength)
    : new Uint8Array(bytes);
  let binary = '';
  for (const byte of view) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** Bytes of a base64url string (padding optional). */
export function fromBase64url(text: string): Uint8Array<ArrayBuffer> {
  const base64 = text.replace(/-/g, '+').replace(/_/g, '/');
  const binary = atob(base64 + '='.repeat((4 - (base64.length % 4)) % 4));
  const bytes = new Uint8Array(new ArrayBuffer(binary.length));
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

interface PublicKeyCredentialStatic {
  parseCreationOptionsFromJSON?: (json: Json) => PublicKeyCredentialCreationOptions;
  parseRequestOptionsFromJSON?: (json: Json) => PublicKeyCredentialRequestOptions;
  isConditionalMediationAvailable?: () => Promise<boolean>;
}

function credentialClass(scope: unknown = globalThis): PublicKeyCredentialStatic | null {
  const cls = (scope as { PublicKeyCredential?: PublicKeyCredentialStatic }).PublicKeyCredential;
  return cls ?? null;
}

/** The browser can do WebAuthn at all (secure context, API present). */
export function webauthnSupported(scope: unknown = globalThis): boolean {
  const s = scope as { isSecureContext?: boolean; navigator?: { credentials?: unknown } };
  return !!credentialClass(scope) && s.isSecureContext !== false && !!s.navigator?.credentials;
}

/** Passkeys can be offered in the username field's autofill (conditional mediation). */
export async function conditionalMediationAvailable(scope: unknown = globalThis): Promise<boolean> {
  const cls = credentialClass(scope);
  if (!cls || typeof cls.isConditionalMediationAvailable !== 'function') return false;
  try {
    return await cls.isConditionalMediationAvailable();
  } catch {
    return false;
  }
}

function descriptors(list: unknown): PublicKeyCredentialDescriptor[] {
  return Array.isArray(list)
    ? list.map((item: Json) => ({
        type: 'public-key',
        id: fromBase64url(String(item.id)),
        ...(Array.isArray(item.transports)
          ? { transports: item.transports as AuthenticatorTransport[] }
          : {}),
      }))
    : [];
}

/** `PublicKeyCredentialCreationOptions` from the JSON Identity sends. */
export function creationOptions(
  json: Json,
  scope: unknown = globalThis,
): PublicKeyCredentialCreationOptions {
  const native = credentialClass(scope)?.parseCreationOptionsFromJSON;
  if (typeof native === 'function') return native(json);
  const user = json.user as Json;
  return {
    ...(json as unknown as PublicKeyCredentialCreationOptions),
    challenge: fromBase64url(String(json.challenge)),
    user: {
      id: fromBase64url(String(user.id)),
      name: String(user.name),
      displayName: String(user.displayName ?? user.name),
    },
    excludeCredentials: descriptors(json.excludeCredentials),
  };
}

/** `PublicKeyCredentialRequestOptions` from the JSON Identity sends. */
export function requestOptions(
  json: Json,
  scope: unknown = globalThis,
): PublicKeyCredentialRequestOptions {
  const native = credentialClass(scope)?.parseRequestOptionsFromJSON;
  if (typeof native === 'function') return native(json);
  return {
    ...(json as unknown as PublicKeyCredentialRequestOptions),
    challenge: fromBase64url(String(json.challenge)),
    allowCredentials: descriptors(json.allowCredentials),
  };
}

interface CredentialLike {
  id: string;
  rawId: ArrayBuffer;
  type: string;
  authenticatorAttachment?: string | null;
  response: {
    clientDataJSON: ArrayBuffer;
    attestationObject?: ArrayBuffer;
    authenticatorData?: ArrayBuffer;
    signature?: ArrayBuffer;
    userHandle?: ArrayBuffer | null;
    getTransports?: () => string[];
  };
  getClientExtensionResults?: () => unknown;
  toJSON?: () => Json;
}

/** `PublicKeyCredential.toJSON()`, also where the browser lacks it. */
export function credentialJson(credential: Credential): Json {
  const cred = credential as unknown as CredentialLike;
  if (typeof cred.toJSON === 'function') return cred.toJSON();
  const r = cred.response;
  const response: Json = { clientDataJSON: toBase64url(r.clientDataJSON) };
  if (r.attestationObject) {
    response.attestationObject = toBase64url(r.attestationObject);
    response.transports = typeof r.getTransports === 'function' ? r.getTransports() : [];
  }
  if (r.authenticatorData) response.authenticatorData = toBase64url(r.authenticatorData);
  if (r.signature) response.signature = toBase64url(r.signature);
  if (r.userHandle) response.userHandle = toBase64url(r.userHandle);
  return {
    id: cred.id,
    rawId: toBase64url(cred.rawId),
    type: cred.type,
    authenticatorAttachment: cred.authenticatorAttachment ?? null,
    response,
    clientExtensionResults:
      typeof cred.getClientExtensionResults === 'function' ? cred.getClientExtensionResults() : {},
  };
}

/** The user dismissed the browser's passkey dialog, or a newer request aborted this one. */
export function cancelled(error: unknown): boolean {
  const name = (error as { name?: string } | null)?.name;
  return name === 'NotAllowedError' || name === 'AbortError';
}

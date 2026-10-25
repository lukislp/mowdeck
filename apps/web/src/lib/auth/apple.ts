/**
 * Sign in with Apple (ADR 0052): the round trip of `oauth.ts` for the provider `apple`.
 */
import {
  linkProvider,
  linkResultOf,
  providerEnabled,
  signInResultOf,
  signInWith,
  type OAuthDeps,
  type Query,
} from './oauth';

export const PROVIDER = 'apple';

/** Whether the installation offers Apple (`GET /login-methods`); never rejects. */
export function appleEnabled(fetchFn?: typeof fetch): Promise<boolean> {
  return providerEnabled(PROVIDER, fetchFn);
}

/** Start signing in with Apple; resolves when the browser is on its way. Throws `ApiProblem`. */
export function signInWithApple(params: Query, deps?: OAuthDeps): Promise<void> {
  return signInWith(PROVIDER, params, deps);
}

/** Start linking Apple to the signed-in user (`start`: the typed API call with the password). */
export const linkApple = linkProvider;

/** What came back from Apple to the sign-in page: signed in, an error, or nothing. */
export const appleSignInResult = (params: Query): ReturnType<typeof signInResultOf> =>
  signInResultOf(PROVIDER, params);

/** What came back from Apple to the settings: linked, an error, or nothing. */
export const appleLinkResult = (params: Query): ReturnType<typeof linkResultOf> =>
  linkResultOf(PROVIDER, params);

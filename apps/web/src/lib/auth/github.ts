/**
 * Sign in with GitHub (ADR 0051): the round trip of `oauth.ts` for the provider `github`.
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

export { returnQuery, withoutResult, type LinkError, type SignInError } from './oauth';

export const PROVIDER = 'github';

export type GitHubDeps = OAuthDeps;

/** Whether the installation offers GitHub (`GET /login-methods`); never rejects. */
export function githubEnabled(fetchFn?: typeof fetch): Promise<boolean> {
  return providerEnabled(PROVIDER, fetchFn);
}

/** Start signing in with GitHub; resolves when the browser is on its way. Throws `ApiProblem`. */
export function signInWithGitHub(params: Query, deps?: GitHubDeps): Promise<void> {
  return signInWith(PROVIDER, params, deps);
}

/** Start linking GitHub to the signed-in user (`start`: the typed API call with the password). */
export const linkGitHub = linkProvider;

/** What came back from GitHub to the sign-in page: signed in, an error, or nothing. */
export const signInResult = (params: Query): ReturnType<typeof signInResultOf> =>
  signInResultOf(PROVIDER, params);

/** What came back from GitHub to the settings: linked, an error, or nothing. */
export const linkResult = (params: Query): ReturnType<typeof linkResultOf> =>
  linkResultOf(PROVIDER, params);

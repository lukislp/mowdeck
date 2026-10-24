<!--
  Sign-in: a passkey (ADR 0050) or username and password.

  - Browser: "Sign in with a passkey" runs the WebAuthn login without a username; where the
    browser supports it, the passkeys are also offered in the username field's autofill
    (conditional UI, started when the page opens).
  - iOS/Android app: the web view cannot reach the passkeys, so the button opens this page in the
    system browser and takes the login over (`#lib/native/handoff.ts`).
  - This page opened by the app (`?app_challenge=...&app_state=...`): any login here is handed to
    the app as a one-time code and the browser returns to `mowdeck://auth/callback`.
-->
<script lang="ts">
  import { Button, Field, Icon, ProblemNotice } from '@mowdeck/ui';
  import { onMount } from 'svelte';

  import { page } from '$app/state';

  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { passkeysEnabled, signInWithPasskey } from '#lib/auth/passkey.js';
  import { IDENTITY, session } from '#lib/auth/session.svelte.js';
  import {
    cancelled,
    conditionalMediationAvailable,
    webauthnSupported,
  } from '#lib/auth/webauthn.js';
  import AuthFrame from '#lib/components/AuthFrame.svelte';
  import { loadConfig } from '#lib/config.js';
  import { problemMessage } from '#lib/i18n.js';
  import { appLoginRequest, callbackUrl, HandoffError } from '#lib/native/handoff.js';
  import { nativeShell } from '#lib/native/runtime.js';
  import { navigatePath } from '#lib/navigate.js';
  import { m } from '#lib/paraglide/messages.js';

  let username = $state('');
  let password = $state('');
  let busy = $state(false);
  let passkeyBusy = $state(false);
  let problem = $state<ApiProblem | null>(null);
  let notice = $state<string | null>(null);
  let usedPasskey = $state(false);

  /** The browser was opened by the app for this login. */
  const appRequest = $derived(appLoginRequest(page.url.searchParams));
  /** After the hand-off: the link back to the app. */
  let returnLink = $state<string | null>(null);

  // Passkeys: on in the installation, and possible here (browser support, or the app's
  // system-browser hand-off).
  let passkeysOn = $state(false);
  const inApp = nativeShell.active;
  const passkeyButton = $derived(
    passkeysOn &&
      (inApp
        ? nativeShell.canSignInThroughBrowser
        : typeof window !== 'undefined' && webauthnSupported(window)),
  );
  let conditional: AbortController | null = null;
  let waiting = $state<AbortController | null>(null);

  const next = $derived.by(() => {
    const target = page.url.searchParams.get('next') ?? '/mowers';
    return target.startsWith('/') && !target.startsWith('//') ? target : '/mowers';
  });

  $effect(() => {
    // A login for the app never continues into this browser's app.
    if (session.status === 'authenticated' && !appRequest && !busy && !passkeyBusy) {
      void navigatePath(next, { replace: true });
    }
  });

  // Links to the legal pages only when the installation turns them on (`/config.json`).
  let legal = $state(false);
  $effect(() => {
    void loadConfig().then((config) => (legal = config.legalEnabled));
  });

  onMount(() => {
    void passkeysEnabled().then(async (on) => {
      passkeysOn = on;
      if (on && !inApp && webauthnSupported(window) && (await conditionalMediationAvailable())) {
        startConditional();
      }
    });
    return () => conditional?.abort();
  });

  /** Offer the passkeys in the username field's autofill until one is picked. */
  function startConditional(): void {
    conditional = new AbortController();
    const { signal } = conditional;
    void signInWithPasskey(session, { mediation: 'conditional', signal })
      .then(() => finish())
      .catch((error: unknown) => {
        if (signal.aborted || cancelled(error)) return;
        usedPasskey = true;
        problem = asProblem(error);
        notice = null;
      });
  }

  /** After any successful login: into the app, or (opened by the app) back to the app. */
  async function finish(): Promise<void> {
    if (appRequest) {
      try {
        const code = await session.handOffToApp(appRequest.challenge);
        returnLink = callbackUrl(code, appRequest.state);
        window.location.href = returnLink;
      } catch (error) {
        const found = asProblem(error);
        if (found.status === 403) notice = m.login_app_stale();
        else problem = found;
      }
      return;
    }
    password = '';
    await navigatePath(session.me?.must_change_password ? '/settings?password=1#profile' : next, {
      replace: true,
    });
  }

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    conditional?.abort();
    busy = true;
    problem = null;
    notice = null;
    usedPasskey = false;
    try {
      await session.login(username.trim(), password);
      await finish();
    } catch (error) {
      problem = asProblem(error);
    } finally {
      busy = false;
    }
  }

  async function withPasskey(): Promise<void> {
    conditional?.abort();
    passkeyBusy = true;
    problem = null;
    notice = null;
    usedPasskey = true;
    try {
      if (inApp) {
        waiting = new AbortController();
        const result = await nativeShell.signInThroughBrowser(
          window.location.origin,
          (code, verifier) =>
            session.signIn(`${IDENTITY}/app-login/session`, { code, code_verifier: verifier }),
          waiting.signal,
        );
        if (result === 'signed-in') await finish();
      } else {
        await signInWithPasskey(session);
        await finish();
      }
    } catch (error) {
      if (cancelled(error)) return;
      if (error instanceof HandoffError) notice = m.login_app_failed();
      else problem = asProblem(error);
    } finally {
      passkeyBusy = false;
      waiting = null;
    }
  }

  const message = $derived(
    problem === null
      ? null
      : problem.status === 401
        ? usedPasskey
          ? m.login_passkey_failed()
          : m.login_failed()
        : problemMessage(problem),
  );
</script>

<svelte:head><title>{m.login_title()} · {m.app_name()}</title></svelte:head>

<AuthFrame title={m.login_title()}>
  {#if appRequest}
    <p class="app-note" data-testid="login-app-notice">
      <Icon name="phone" size={18} />
      <span>{returnLink ? m.login_app_done() : m.login_app_notice()}</span>
    </p>
    {#if returnLink}
      <Button href={returnLink} variant="primary" full icon="phone" testid="login-app-return"
        >{m.login_app_return()}</Button
      >
    {/if}
  {/if}
  {#if session.ended === 'expired' && !problem && !appRequest}
    <ProblemNotice tone="info" title={m.login_session_ended()} />
  {/if}
  {#if !returnLink}
    {#if passkeyButton}
      <div class="passkey">
        <Button
          variant="primary"
          full
          icon="key"
          pending={passkeyBusy}
          disabled={busy}
          testid="login-passkey"
          onclick={withPasskey}>{m.login_passkey()}</Button
        >
        {#if waiting}
          <p class="waiting" role="status">
            <span>{m.login_app_waiting()}</span>
            <button type="button" class="link" onclick={() => waiting?.abort()}>{m.cancel()}</button
            >
          </p>
        {/if}
      </div>
      <p class="divider" aria-hidden="true"><span>{m.login_divider()}</span></p>
    {/if}
    <form class="form" onsubmit={submit} novalidate>
      <Field
        id="username"
        label={m.login_username()}
        autocomplete={passkeysOn ? 'username webauthn' : 'username'}
        required
        bind:value={username}
      />
      <Field
        id="password"
        type="password"
        label={m.login_password()}
        autocomplete="current-password"
        required
        bind:value={password}
      />
      {#if message && problem}
        <ProblemNotice
          title={message}
          reference={problem.status === 401 ? null : problem.correlationId}
          referenceLabel={m.problem_reference()}
        />
      {:else if notice}
        <ProblemNotice tone="info" title={notice} />
      {/if}
      <Button
        type="submit"
        variant={passkeyButton ? 'secondary' : 'primary'}
        full
        pending={busy}
        disabled={!username || !password || passkeyBusy}
        testid="login-submit">{m.login_submit()}</Button
      >
    </form>
  {/if}
  {#if !appRequest}
    <a class="switch" href="/register">{m.login_register_link()}</a>
  {/if}
  {#if legal}
    <nav class="legal" aria-label={m.legal_nav()} data-testid="legal-links">
      <a href="/legal/imprint" data-testid="legal-link-imprint">{m.legal_imprint()}</a>
      <a href="/legal/terms" data-testid="legal-link-terms">{m.legal_terms()}</a>
      <a href="/legal/privacy" data-testid="legal-link-privacy">{m.legal_privacy()}</a>
    </nav>
  {/if}
</AuthFrame>

<style>
  .form {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-4);
  }

  .passkey {
    display: grid;
    gap: var(--md-space-2);
  }

  .waiting {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-2);
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .link {
    min-height: var(--md-target);
    border: none;
    background: none;
    color: var(--md-color-ink);
    font: inherit;
    font-weight: var(--md-weight-semibold);
    text-decoration: underline;
    cursor: pointer;
  }

  /* A hairline with the label in its gap, like a scale mark on the plate. */
  .divider {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: var(--md-space-3);
    align-items: center;
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .divider::before,
  .divider::after {
    content: '';
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .app-note {
    display: flex;
    gap: var(--md-space-2);
    align-items: flex-start;
    margin: 0;
    padding: var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line);
    border-left: var(--md-border-control) solid var(--md-color-ink);
    background: var(--md-color-surface-sunken);
    font-size: var(--md-font-size-14);
  }

  .switch {
    display: inline-flex;
    align-items: center;
    min-height: var(--md-target);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .legal {
    display: flex;
    flex-wrap: wrap;
    gap: 0 var(--md-space-4);
    padding-top: var(--md-space-2);
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .legal a {
    display: inline-flex;
    align-items: center;
    min-height: var(--md-target);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }
</style>

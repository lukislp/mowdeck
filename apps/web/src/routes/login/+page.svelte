<!--
  Sign-in: a passkey (ADR 0050), GitHub (ADR 0051), Apple (ADR 0052) or username and password.

  - Browser: "Sign in with a passkey" runs the WebAuthn login without a username; where the
    browser supports it, the passkeys are also offered in the username field's autofill
    (conditional UI, started when the page opens).
  - iOS/Android app: the web view cannot reach the passkeys, so the button opens this page in the
    system browser and takes the login over (`#lib/native/handoff.ts`).
  - "Sign in with GitHub" (only when the installation has it and the account is linked in the
    settings) leaves for GitHub and comes back here with `?signed_in_with=github` and the session
    cookie set (or `?sign_in_error=<code>`); the query (`next`, the app hand-off) survives it. In
    the app the button runs the login in the system browser, like the passkey button.
  - This page opened by the app (`?app_challenge=...&app_state=...`): any login here is handed to
    the app as a one-time code and the browser returns to `mowdeck://auth/callback`.
-->
<script lang="ts">
  import { Button, Field, Icon, ProblemNotice } from '@mowdeck/ui';
  import { onMount, tick } from 'svelte';

  import { replaceState } from '$app/navigation';
  import { page } from '$app/state';

  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { appleEnabled, appleSignInResult, signInWithApple } from '#lib/auth/apple.js';
  import {
    githubEnabled,
    signInResult,
    signInWithGitHub,
    withoutResult,
    type SignInError,
  } from '#lib/auth/github.js';
  import { passkeysEnabled, signInWithPasskey } from '#lib/auth/passkey.js';
  import { IDENTITY, session } from '#lib/auth/session.svelte.js';
  import {
    cancelled,
    conditionalMediationAvailable,
    webauthnSupported,
  } from '#lib/auth/webauthn.js';
  import AppleMark from '#lib/components/AppleMark.svelte';
  import AuthFrame from '#lib/components/AuthFrame.svelte';
  import GitHubMark from '#lib/components/GitHubMark.svelte';
  import { loadConfig } from '#lib/config.js';
  import {
    clock,
    demoRefusal,
    isVisitor,
    markVisitor,
    shouldRecreate,
    spokenMinutes,
  } from '#lib/demo/sandbox.js';
  import { demo } from '#lib/demo/state.svelte.js';
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
  let githubBusy = $state(false);
  let appleBusy = $state(false);
  let demoBusy = $state(false);
  /** Demo sandboxes (ADR 0076): every sandbox is taken; seconds until the automatic retry. */
  let fullLeft = $state<number | null>(null);
  let fullTimer: ReturnType<typeof setInterval> | undefined;
  let fullHeading = $state<HTMLElement | null>(null);
  /** Too many sandboxes from this address: minutes to wait (no automatic retry). */
  let busyMinutes = $state<number | null>(null);
  /** A fresh sandbox replaces one that was deleted (started by the page, not the visitor). */
  let recreating = $state(false);
  let recreateTried = false;

  /**
   * "Try the demo" (ADR 0068): the prepared demo account, no registration, no password; with
   * sandboxes (ADR 0076) a new account of the visitor's own. A full demo counts down and tries
   * again by itself.
   */
  async function tryDemo(): Promise<void> {
    stopCountdown();
    // Any start on this page counts: the page never starts another one by itself.
    recreateTried = true;
    demoBusy = true;
    problem = null;
    notice = null;
    busyMinutes = null;
    try {
      await session.tryDemo();
      markVisitor();
      fullLeft = null;
      await finish();
    } catch (error) {
      const found = asProblem(error);
      const refusal = demoRefusal(found);
      recreating = false;
      if (refusal?.kind === 'full') {
        startCountdown(refusal.seconds);
      } else {
        fullLeft = null;
        if (refusal?.kind === 'busy') busyMinutes = Math.ceil(refusal.seconds / 60);
        else if (found.status === 404) notice = m.login_demo_unavailable();
        else problem = found;
      }
    } finally {
      demoBusy = false;
    }
  }

  /** Count down from `seconds` and try again at zero (again and again while it stays full). */
  function startCountdown(seconds: number): void {
    const first = fullLeft === null;
    const until = Date.now() + seconds * 1000;
    fullLeft = seconds;
    fullTimer = setInterval(() => {
      const left = Math.max(0, Math.ceil((until - Date.now()) / 1000));
      fullLeft = left;
      if (left === 0) void tryDemo();
    }, 1000);
    // The state replaces the button the visitor used: the focus goes to its heading, once.
    if (first) void tick().then(() => fullHeading?.focus());
  }

  function stopCountdown(): void {
    clearInterval(fullTimer);
    fullTimer = undefined;
  }

  /** What screen readers hear of the countdown: once a minute, not every tick. */
  const fullSpoken = $derived.by(() => {
    if (fullLeft === null) return '';
    const minutes = spokenMinutes(fullLeft);
    return minutes > 1 ? m.login_demo_full_spoken({ minutes }) : m.login_demo_full_spoken_soon();
  });

  /** The browser was opened by the app for this login. */
  const appRequest = $derived(appLoginRequest(page.url.searchParams));
  /** Signed in for the app: the user confirms before the login goes to the app. */
  let confirming = $state(false);
  let handing = $state(false);
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

  // GitHub (ADR 0051): on in the installation; in the app through the system browser.
  let githubOn = $state(false);
  const githubButton = $derived(githubOn && (!inApp || nativeShell.canSignInThroughBrowser));
  // Apple (ADR 0052): the same round trip.
  let appleOn = $state(false);
  const appleButton = $derived(appleOn && (!inApp || nativeShell.canSignInThroughBrowser));
  /** Back from GitHub or Apple: signed in (the cookie is set) or why not. */
  const fromGitHub = signInResult(page.url.searchParams);
  const fromApple = appleSignInResult(page.url.searchParams);
  const fromProvider = fromGitHub ?? fromApple;
  const backSignedIn = fromProvider !== null && 'signedIn' in fromProvider;
  let githubDone = false;
  const githubMessages: Record<SignInError, () => string> = {
    cancelled: m.login_github_cancelled,
    expired: m.login_github_expired,
    denied: m.login_github_denied,
    unavailable: m.login_github_unavailable,
    rate_limited: m.login_github_rate_limited,
  };
  const appleMessages: Record<SignInError, () => string> = {
    cancelled: m.login_apple_cancelled,
    expired: m.login_apple_expired,
    denied: m.login_apple_denied,
    unavailable: m.login_apple_unavailable,
    rate_limited: m.login_apple_rate_limited,
  };
  if (fromGitHub && 'error' in fromGitHub) notice = githubMessages[fromGitHub.error]();
  if (fromApple && 'error' in fromApple) notice = appleMessages[fromApple.error]();

  const next = $derived.by(() => {
    const target = page.url.searchParams.get('next') ?? '/mowers';
    return target.startsWith('/') && !target.startsWith('//') ? target : '/mowers';
  });

  $effect(() => {
    // A login for the app never continues into this browser's app.
    if (
      session.status === 'authenticated' &&
      !appRequest &&
      !busy &&
      !passkeyBusy &&
      !backSignedIn
    ) {
      void navigatePath(next, { replace: true });
    }
  });

  $effect(() => {
    // Back from GitHub or Apple: the session comes from the cookie Identity set (restored on load).
    if (!backSignedIn || githubDone) return;
    if (session.status === 'authenticated') {
      githubDone = true;
      replaceState(withoutResult(page.url), page.state);
      void finish();
    } else if (session.status === 'anonymous') {
      githubDone = true;
      notice = m.login_github_expired();
    }
  });

  $effect(() => {
    // Demo sandboxes (ADR 0076): this browser's sandbox is gone (deleted after the visitor was
    // away, the demo was reset, the cookie expired), and the visitor did not log out: start a
    // fresh one, as "Try the demo" would, and go on to `next` instead of showing an error.
    if (!demo.loaded || demoBusy) return;
    const start = shouldRecreate({
      sandbox: demo.config?.sandbox !== undefined,
      visitor: isVisitor(),
      status: session.status,
      appRequest: appRequest !== null,
      tried: recreateTried,
    });
    if (!start) return;
    recreateTried = true;
    recreating = true;
    void tryDemo();
  });

  // Links to the legal pages only when the installation turns them on (`/config.json`).
  let legal = $state(false);
  $effect(() => {
    void loadConfig().then((config) => (legal = config.legalEnabled));
  });

  onMount(() => {
    if (!demo.loaded) void demo.load();
    void githubEnabled().then((on) => (githubOn = on));
    void appleEnabled().then((on) => (appleOn = on));
    if ((fromGitHub && 'error' in fromGitHub) || (fromApple && 'error' in fromApple)) {
      replaceState(withoutResult(page.url), page.state);
    }
    void passkeysEnabled().then(async (on) => {
      passkeysOn = on;
      if (on && !inApp && webauthnSupported(window) && (await conditionalMediationAvailable())) {
        startConditional();
      }
    });
    return () => {
      conditional?.abort();
      stopCountdown();
    };
  });

  /** Offer the passkeys in the username field's autofill until one is picked. */
  function startConditional(): void {
    conditional = new AbortController();
    const { signal } = conditional;
    // The autofill offer is a background attempt: until the user has picked a passkey, a failure
    // (the browser cannot run it, the challenge call failed, a navigation cut it off) says
    // nothing to them and must not replace the page's message (e.g. the result of a GitHub or
    // Apple round trip) with a "no connection" notice.
    let chosen = false;
    void signInWithPasskey(session, {
      mediation: 'conditional',
      signal,
      onChosen: () => (chosen = true),
    })
      .then(() => finish())
      .catch((error: unknown) => {
        if (!chosen || signal.aborted || cancelled(error)) return;
        usedPasskey = true;
        problem = asProblem(error);
        notice = null;
      });
  }

  /** After any successful login: into the app, or (opened by the app) back to the app. */
  async function finish(): Promise<void> {
    if (appRequest) {
      confirming = true;
      return;
    }
    password = '';
    await navigatePath(session.me?.must_change_password ? '/settings?password=1#profile' : next, {
      replace: true,
    });
  }

  /** The user confirmed: the login goes to the app (a tap, which Android needs for the jump). */
  async function handOver(): Promise<void> {
    if (!appRequest) return;
    handing = true;
    problem = null;
    notice = null;
    try {
      const code = await session.handOffToApp(appRequest.requestId);
      returnLink = callbackUrl({ code, state: appRequest.state }, navigator.userAgent);
      window.location.href = returnLink;
    } catch (error) {
      const found = asProblem(error);
      confirming = false;
      if (found.status === 403) notice = m.login_app_stale();
      else problem = found;
    } finally {
      handing = false;
    }
  }

  /** Not for the app after all: end this browser login and tell the app. */
  async function cancelHandOver(): Promise<void> {
    if (!appRequest) return;
    await session.logout();
    confirming = false;
    window.location.href = callbackUrl(
      { error: 'cancelled', state: appRequest.state },
      navigator.userAgent,
    );
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

  /** In the app: any login runs in the system browser and is handed back (ADR 0050). */
  async function throughBrowser(): Promise<void> {
    waiting = new AbortController();
    try {
      const result = await nativeShell.signInThroughBrowser(
        window.location.origin,
        (code, verifier) =>
          session.signIn(`${IDENTITY}/app-login/session`, { code, code_verifier: verifier }),
        waiting.signal,
      );
      if (result === 'signed-in') await finish();
    } finally {
      waiting = null;
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
        await throughBrowser();
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
    }
  }

  /** Leave for GitHub (keeps `next` and the app hand-off); in the app, the system browser. */
  async function withGitHub(): Promise<void> {
    conditional?.abort();
    githubBusy = true;
    problem = null;
    notice = null;
    usedPasskey = false;
    try {
      if (inApp) {
        await throughBrowser();
        githubBusy = false;
      } else {
        // The browser leaves for GitHub: the button stays busy until it is gone.
        await signInWithGitHub(page.url.searchParams);
      }
    } catch (error) {
      githubBusy = false;
      if (cancelled(error)) return;
      if (error instanceof HandoffError) notice = m.login_app_failed();
      else problem = asProblem(error);
    }
  }

  /** Leave for Apple (keeps `next` and the app hand-off); in the app, the system browser. */
  async function withApple(): Promise<void> {
    conditional?.abort();
    appleBusy = true;
    problem = null;
    notice = null;
    usedPasskey = false;
    try {
      if (inApp) {
        await throughBrowser();
        appleBusy = false;
      } else {
        // The browser leaves for Apple: the button stays busy until it is gone.
        await signInWithApple(page.url.searchParams);
      }
    } catch (error) {
      appleBusy = false;
      if (cancelled(error)) return;
      if (error instanceof HandoffError) notice = m.login_app_failed();
      else problem = asProblem(error);
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

<AuthFrame title={demo.config ? m.login_demo_title() : m.login_title()}>
  {#if !demo.loaded}
    <!-- the installation's config decides between the demo entry and the sign-in forms -->
  {:else if demo.config}
    {@const sandbox = demo.config.sandbox}
    <section class="demo" data-testid="login-demo" data-mode={sandbox ? 'sandbox' : 'shared'}>
      <span class="badge" data-testid="login-demo-badge">{m.demo_badge()}</span>
      {#if fullLeft !== null}
        <!-- Every sandbox is taken (503 demo-full): count down, then try again by itself. -->
        <div class="full" data-testid="login-demo-full">
          <h2 class="full-title" tabindex="-1" bind:this={fullHeading}>
            {m.login_demo_full_title()}
          </h2>
          <p class="full-text">{m.login_demo_full_text()}</p>
          <div class="readout">
            <span class="readout-label">{m.login_demo_full_next()}</span>
            <span class="readout-value" aria-hidden="true" data-testid="login-demo-countdown"
              >{clock(fullLeft)}</span
            >
            <span class="md-visually-hidden" role="status">{fullSpoken}</span>
          </div>
          <Button
            variant="secondary"
            full
            pending={demoBusy}
            testid="login-demo-retry"
            onclick={tryDemo}>{m.login_demo_full_retry()}</Button
          >
        </div>
      {:else if recreating}
        <div class="recreating" role="status" data-testid="login-demo-recreating">
          <span class="sweep" aria-hidden="true"></span>
          <p class="recreating-title">{m.login_demo_recreating()}</p>
          <p class="note">{m.login_demo_recreating_hint()}</p>
        </div>
      {:else}
        <p class="lead">{m.login_demo_lead()}</p>
        <ul class="points">
          <li>{m.login_demo_point_drive()}</li>
          <li>{m.login_demo_point_maps()}</li>
          <li>{m.login_demo_point_plan()}</li>
          {#if demo.apiExplorer}<li>{m.login_demo_point_api()}</li>{/if}
        </ul>
        {#if problem}
          <ProblemNotice
            title={problemMessage(problem)}
            reference={problem.correlationId}
            referenceLabel={m.problem_reference()}
          />
        {:else if busyMinutes !== null}
          <div data-testid="login-demo-busy">
            <ProblemNotice tone="info" title={m.login_demo_busy({ minutes: busyMinutes })} />
          </div>
        {:else if notice}
          <ProblemNotice tone="info" title={notice} />
        {/if}
        <Button
          variant="primary"
          full
          pending={demoBusy}
          testid="login-demo-start"
          onclick={tryDemo}>{m.login_demo_button()}</Button
        >
        <p class="note" data-testid="login-demo-note">
          {sandbox
            ? m.login_demo_note_sandbox({
                minutes: sandbox.idleMinutes,
                hours: demo.config.resetHours,
              })
            : m.login_demo_note({ hours: demo.config.resetHours })}
        </p>
      {/if}
    </section>
  {:else}
    {#if appRequest}
      <p class="app-note" data-testid="login-app-notice">
        <Icon name="phone" size={18} />
        <span>{returnLink ? m.login_app_done() : m.login_app_notice()}</span>
      </p>
      {#if returnLink}
        <Button href={returnLink} variant="primary" full icon="phone" testid="login-app-return"
          >{m.login_app_return()}</Button
        >
      {:else if confirming}
        <div class="confirm" data-testid="login-app-confirm">
          <p>
            {m.login_app_confirm({ name: session.me?.display_name || session.me?.username || '' })}
          </p>
          <Button
            variant="primary"
            full
            icon="phone"
            pending={handing}
            testid="login-app-continue"
            onclick={handOver}>{m.login_app_continue()}</Button
          >
          <Button variant="ghost" full disabled={handing} onclick={cancelHandOver}
            >{m.cancel()}</Button
          >
        </div>
      {/if}
    {/if}
    {#if session.ended === 'expired' && !problem && !appRequest}
      <ProblemNotice tone="info" title={m.login_session_ended()} />
    {/if}
    {#if !returnLink && !confirming}
      {#if passkeyButton || githubButton || appleButton}
        <div class="passkey">
          {#if passkeyButton}
            <Button
              variant="primary"
              full
              icon="key"
              pending={passkeyBusy}
              disabled={busy || githubBusy || appleBusy}
              testid="login-passkey"
              onclick={withPasskey}>{m.login_passkey()}</Button
            >
          {/if}
          {#if githubButton}
            <Button
              variant={passkeyButton ? 'secondary' : 'primary'}
              full
              pending={githubBusy}
              disabled={busy || passkeyBusy || appleBusy}
              testid="login-github"
              onclick={withGitHub}
              ><span class="provider"><GitHubMark />{m.login_github()}</span></Button
            >
          {/if}
          {#if appleButton}
            <Button
              variant={passkeyButton || githubButton ? 'secondary' : 'primary'}
              full
              pending={appleBusy}
              disabled={busy || passkeyBusy || githubBusy}
              testid="login-apple"
              onclick={withApple}
              ><span class="provider"><AppleMark />{m.login_apple()}</span></Button
            >
          {/if}
          {#if waiting}
            <p class="waiting" role="status">
              <span>{m.login_app_waiting()}</span>
              <button type="button" class="link" onclick={() => waiting?.abort()}
                >{m.cancel()}</button
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
          variant={passkeyButton || githubButton || appleButton ? 'secondary' : 'primary'}
          full
          pending={busy}
          disabled={!username || !password || passkeyBusy || githubBusy || appleBusy}
          testid="login-submit">{m.login_submit()}</Button
        >
      </form>
    {/if}
    {#if !appRequest}
      <a class="switch" href="/register">{m.login_register_link()}</a>
    {/if}
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
  /* The demo entry (ADR 0068): the tag of the demo band, three numbered facts, one button. */
  .demo {
    display: grid;
    gap: var(--md-space-4);
  }

  .badge {
    justify-self: start;
    padding: var(--md-space-1) var(--md-space-3);
    padding-inline-end: 30px;
    background:
      linear-gradient(var(--md-hud-lit), var(--md-hud-lit)) left / calc(100% - 22px) 100% no-repeat,
      repeating-linear-gradient(-55deg, var(--md-hud-lit) 0 5px, transparent 5px 10px);
    color: var(--md-hud-ink-inverse);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: 0.24em;
    text-transform: uppercase;
  }

  .lead {
    margin: 0;
    font-size: var(--md-font-size-16);
  }

  .points {
    display: grid;
    gap: 0;
    margin: 0;
    padding: 0;
    border-top: var(--md-hairline) solid var(--md-color-line);
    list-style: none;
    counter-reset: point;
  }

  .points li {
    display: grid;
    grid-template-columns: 3ch 1fr;
    gap: var(--md-space-3);
    padding: var(--md-space-2) 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    font-size: var(--md-font-size-14);
    counter-increment: point;
  }

  .points li::before {
    content: counter(point, decimal-leading-zero);
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
  }

  .note {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  /* "The demo is full" (ADR 0076): a plate under the demo tape with one mono readout, like the
     countdown of a test rig that waits for a free bay. */
  .full {
    display: grid;
    gap: var(--md-space-3);
    padding: var(--md-space-4);
    padding-top: calc(var(--md-space-4) + 6px);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background:
      repeating-linear-gradient(-55deg, var(--md-hud-lit) 0 5px, transparent 5px 10px) top / 100%
        6px no-repeat,
      var(--md-color-surface);
  }

  .full-title {
    margin: 0;
    color: var(--md-color-ink);
    font-size: var(--md-font-size-20);
    font-weight: var(--md-weight-semibold);
    line-height: var(--md-line-28);
    letter-spacing: normal;
    text-transform: none;
  }

  .full-title:focus {
    outline: none;
  }

  .full-title:focus-visible {
    outline: var(--md-border-control) solid var(--md-color-focus);
    outline-offset: 4px;
  }

  .full-text {
    margin: 0;
    font-size: var(--md-font-size-14);
  }

  .readout {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2) var(--md-space-4);
    align-items: baseline;
    justify-content: space-between;
    padding: var(--md-space-2) 0;
    border-block: var(--md-hairline) solid var(--md-color-line);
  }

  .readout-label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .readout-value {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-32);
    font-weight: var(--md-weight-medium);
    font-variant-numeric: tabular-nums;
    line-height: var(--md-line-32);
    letter-spacing: 0.04em;
  }

  /* A fresh sandbox replaces a deleted one: a scan line runs over a hairline scale. */
  .recreating {
    display: grid;
    gap: var(--md-space-2);
    padding: var(--md-space-4) 0;
  }

  .recreating-title {
    margin: 0;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-16);
    font-weight: var(--md-weight-medium);
  }

  .sweep {
    position: relative;
    height: 6px;
    overflow: hidden;
    border-bottom: var(--md-hairline) solid var(--md-color-line-strong);
  }

  .sweep::after {
    content: '';
    position: absolute;
    inset: 0 auto 0 0;
    width: 30%;
    background: repeating-linear-gradient(-55deg, var(--md-hud-lit) 0 5px, transparent 5px 10px);
    animation: sweep 1.4s var(--md-ease) infinite alternate;
  }

  @keyframes sweep {
    to {
      transform: translateX(233%);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .sweep::after {
      width: 100%;
      animation: none;
    }
  }

  .form {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-4);
  }

  .passkey {
    display: grid;
    gap: var(--md-space-2);
  }

  .provider {
    display: inline-flex;
    gap: var(--md-space-2);
    align-items: center;
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

  .confirm {
    display: grid;
    gap: var(--md-space-3);
  }

  .confirm p {
    margin: 0;
    font-size: var(--md-font-size-16);
    font-weight: var(--md-weight-semibold);
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

<!--
  Security (ADR 0065): one overview of how the signed-in user's account is protected. On top a
  strip of five gauges (password, passkeys, login providers, personal access tokens, signed-in
  devices), each a jump to its part. Then the signed-in devices with "sign out" per device and
  "sign out everywhere else" (both ask for a step-up), the new-login alert switch, the passkeys
  and login providers (moved here from the profile), the active personal access tokens with an
  inline revoke, and the security events of the last 30 days as a timeline.

  The new-login notification links to `/settings/security?revoke=<session>`: that session is
  flagged at the top with a one-tap "sign this device out".
-->
<script lang="ts">
  import { Button, Icon, Panel, ProblemNotice, Switch } from '@mowdeck/ui';

  import { goto } from '$app/navigation';
  import { page } from '$app/state';

  import { identityApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import LoginLinksPanel from '#lib/components/LoginLinksPanel.svelte';
  import PasskeysPanel from '#lib/components/PasskeysPanel.svelte';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import DemoLock from '#lib/demo/DemoLock.svelte';
  import { formatAge, formatDate, formatDateTime, formatClock, problemMessage } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';
  import { daysLeft, isActive, type PersonalToken } from '#lib/settings/personal-tokens.js';
  import {
    EVENT_DAYS,
    REVOKE_PARAM,
    deviceIcon,
    deviceText,
    eventTitle,
    eventTone,
    groupByDay,
    methodText,
    orderSessions,
    revokeTarget,
    whereText,
    type SecurityEvent,
    type SecurityOverview,
    type SessionInfo,
  } from '#lib/settings/security.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  let overview = $state<SecurityOverview | null>(null);
  let sessions = $state<SessionInfo[] | null>(null);
  let events = $state<SecurityEvent[] | null>(null);
  let tokens = $state<PersonalToken[] | null>(null);
  let problem = $state<ApiProblem | null>(null);
  let now = $state(Date.now());

  /** The session whose sign-out waits for a second tap. */
  let confirming = $state<string | null>(null);
  let confirmingOthers = $state(false);
  let working = $state<string | null>(null);
  let alertsBusy = $state(false);

  const flaggedId = $derived(revokeTarget(page.url.searchParams));
  const flagged = $derived(
    flaggedId && sessions ? (sessions.find((s) => s.id === flaggedId) ?? null) : null,
  );
  const others = $derived((sessions ?? []).filter((s) => !s.current));
  const activeTokens = $derived((tokens ?? []).filter((t) => isActive(t, now)));
  const days = $derived(groupByDay(events ?? []));

  async function loadOverview(): Promise<void> {
    overview = await unwrap(identityApi.GET('/api/v1/identity/me/security'));
  }

  async function loadSessions(): Promise<void> {
    sessions = orderSessions(await unwrap(identityApi.GET('/api/v1/identity/me/sessions')));
  }

  async function loadEvents(): Promise<void> {
    events = await unwrap(
      identityApi.GET('/api/v1/identity/me/security-events', {
        params: { query: { days: EVENT_DAYS } },
      }),
    );
  }

  async function loadTokens(): Promise<void> {
    tokens = await unwrap(identityApi.GET('/api/v1/identity/me/api-tokens'));
  }

  async function load(): Promise<void> {
    now = Date.now();
    try {
      await Promise.all([loadOverview(), loadSessions(), loadEvents(), loadTokens()]);
      problem = null;
    } catch (error) {
      problem = asProblem(error);
    }
  }

  $effect(() => {
    void load();
  });

  /** Drop `?revoke=` once handled, so a reload does not flag the session again. */
  function clearFlag(): void {
    if (!page.url.searchParams.has(REVOKE_PARAM)) return;
    const url = new URL(page.url.href);
    url.searchParams.delete(REVOKE_PARAM);
    void goto(url, { shallow: true, replace: true, state: page.state });
  }

  async function revoke(session: SessionInfo): Promise<void> {
    working = session.id;
    try {
      await unwrap(
        identityApi.DELETE('/api/v1/identity/me/sessions/{session_id}', {
          params: { path: { session_id: session.id } },
        }),
      );
      toasts.push('ok', m.sec_session_revoked({ device: deviceText(session) }));
      confirming = null;
      if (session.id === flaggedId) clearFlag();
      await Promise.all([loadSessions(), loadOverview(), loadEvents()]);
    } catch (error) {
      toasts.push('error', problemMessage(asProblem(error)));
    } finally {
      working = null;
    }
  }

  async function revokeOthers(): Promise<void> {
    working = 'others';
    try {
      const result = await unwrap(identityApi.POST('/api/v1/identity/me/sessions/revoke-others'));
      toasts.push('ok', m.sec_others_revoked({ count: result.revoked }));
      confirmingOthers = false;
      clearFlag();
      await Promise.all([loadSessions(), loadOverview(), loadEvents()]);
    } catch (error) {
      toasts.push('error', problemMessage(asProblem(error)));
    } finally {
      working = null;
    }
  }

  async function setAlerts(on: boolean): Promise<void> {
    if (!overview) return;
    alertsBusy = true;
    try {
      const result = await unwrap(
        identityApi.PUT('/api/v1/identity/me/security/alerts', {
          body: { new_login_alerts: on },
        }),
      );
      overview = { ...overview, new_login_alerts: result.new_login_alerts };
      toasts.push(
        'ok',
        result.new_login_alerts ? m.sec_alerts_on_toast() : m.sec_alerts_off_toast(),
      );
      await loadEvents();
    } catch (error) {
      // Dismissed step-up or a failure: the switch goes back.
      overview = { ...overview };
      const found = asProblem(error);
      if (found.status !== 401) toasts.push('error', problemMessage(found));
    } finally {
      alertsBusy = false;
    }
  }

  async function revokeToken(token: PersonalToken): Promise<void> {
    working = token.id;
    try {
      await unwrap(
        identityApi.DELETE('/api/v1/identity/me/api-tokens/{token_id}', {
          params: { path: { token_id: token.id } },
        }),
      );
      toasts.push('ok', m.sec_token_revoked({ name: token.name }));
      await Promise.all([loadTokens(), loadOverview(), loadEvents()]);
    } catch (error) {
      toasts.push('error', problemMessage(asProblem(error)));
    } finally {
      working = null;
    }
  }

  function providerList(providers: string[]): string {
    if (providers.length === 0) return m.sec_none();
    return providers
      .map((p) => (p === 'github' ? 'GitHub' : p === 'apple' ? 'Apple' : p))
      .join(' · ');
  }
</script>

<p class="pad intro">{m.sec_intro()}</p>

{#if problem && !overview}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      detail={problem.detail}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if !overview || !sessions}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{:else}
  <ol class="posture" data-testid="security-posture" aria-label={m.sec_posture()}>
    <li>
      <a class="gauge" href="#profile" data-testid="security-gauge-password">
        <span class="g-label">{m.sec_gauge_password()}</span>
        <span class="g-value" class:warn={!overview.password_set || overview.must_change_password}>
          {overview.must_change_password
            ? m.sec_password_must_change()
            : overview.password_set
              ? m.sec_password_set()
              : m.sec_password_unset()}
        </span>
        <span class="g-meta">
          {overview.password_changed_at
            ? m.sec_password_changed({ date: formatDate(overview.password_changed_at) })
            : m.sec_password_changed_unknown()}
        </span>
      </a>
    </li>
    <li>
      <a class="gauge" href="#security-passkeys" data-testid="security-gauge-passkeys">
        <span class="g-label">{m.sec_gauge_passkeys()}</span>
        <span class="g-value num">{overview.passkeys}</span>
        <span class="g-meta"
          >{overview.passkeys ? m.sec_passkeys_some() : m.sec_passkeys_none()}</span
        >
      </a>
    </li>
    <li>
      <a class="gauge" href="#security-providers" data-testid="security-gauge-providers">
        <span class="g-label">{m.sec_gauge_providers()}</span>
        <span class="g-value num">{overview.providers.length}</span>
        <span class="g-meta">{providerList(overview.providers)}</span>
      </a>
    </li>
    <li>
      <a class="gauge" href="#security-tokens" data-testid="security-gauge-tokens">
        <span class="g-label">{m.sec_gauge_tokens()}</span>
        <span class="g-value num">{overview.personal_tokens}</span>
        <span class="g-meta">{m.sec_tokens_active()}</span>
      </a>
    </li>
    <li>
      <a class="gauge" href="#security-devices" data-testid="security-gauge-sessions">
        <span class="g-label">{m.sec_gauge_sessions()}</span>
        <span class="g-value num">{overview.sessions}</span>
        <span class="g-meta">{m.sec_sessions_signed_in()}</span>
      </a>
    </li>
  </ol>

  {#if flagged}
    <div class="flag" role="alert" data-testid="security-flagged">
      <Icon name="alert" size={20} />
      <div class="flag-text">
        <strong>{m.sec_flag_title({ device: deviceText(flagged) })}</strong>
        <span>
          {whereText(flagged.ip_prefix, flagged.network)} · {formatDateTime(flagged.created_at)}
        </span>
        <span>{flagged.current ? m.sec_flag_current() : m.sec_flag_question()}</span>
      </div>
      <div class="flag-actions">
        {#if !flagged.current}
          <Button
            variant="danger"
            icon="logout"
            pending={working === flagged.id}
            testid="security-flagged-revoke"
            onclick={() => revoke(flagged)}>{m.sec_flag_revoke()}</Button
          >
        {/if}
        <Button variant="ghost" testid="security-flagged-mine" onclick={clearFlag}
          >{m.sec_flag_mine()}</Button
        >
      </div>
    </div>
  {:else if flaggedId}
    <p class="pad quiet" data-testid="security-flagged-gone">{m.sec_flag_gone()}</p>
  {/if}

  <div id="security-devices">
    <Panel label={m.sec_devices_title()} testid="security-sessions">
      <ol class="sessions">
        {#each sessions as session (session.id)}
          <li
            class="session"
            class:current={session.current}
            class:flagged={session.id === flaggedId}
            data-testid="session-{session.current ? 'current' : session.id}"
          >
            <span class="mark" aria-hidden="true"
              ><Icon name={deviceIcon(session.client)} size={20} /></span
            >
            <div class="what">
              <span class="device">
                {deviceText(session)}
                {#if session.current}<span class="tag">{m.sec_this_device()}</span>{/if}
              </span>
              <span class="meta">
                {whereText(session.ip_prefix, session.network)}{#if methodText(session.method)}
                  · {methodText(session.method)}{/if}
              </span>
              <span class="meta times">
                {m.sec_since({ date: formatDate(session.created_at) })} · {m.sec_active({
                  age: formatAge(session.last_used_at, now),
                })}
              </span>
            </div>
            {#if !session.current}
              <div class="act">
                {#if confirming === session.id}
                  <Button
                    variant="danger"
                    pending={working === session.id}
                    testid="session-revoke-confirm"
                    onclick={() => revoke(session)}>{m.sec_revoke_confirm()}</Button
                  >
                  <Button variant="ghost" onclick={() => (confirming = null)}>{m.cancel()}</Button>
                {:else}
                  <Button
                    variant="ghost"
                    icon="logout"
                    testid="session-revoke-{session.id}"
                    onclick={() => (confirming = session.id)}>{m.sec_revoke()}</Button
                  >
                {/if}
              </div>
            {/if}
          </li>
        {/each}
      </ol>
      <div class="foot">
        {#if others.length > 0}
          {#if confirmingOthers}
            <p class="confirm-text">{m.sec_others_confirm_text({ count: others.length })}</p>
            <div class="row">
              <Button
                variant="danger"
                icon="logout"
                pending={working === 'others'}
                testid="sessions-revoke-others-confirm"
                onclick={revokeOthers}>{m.sec_others_confirm({ count: others.length })}</Button
              >
              <Button variant="ghost" onclick={() => (confirmingOthers = false)}
                >{m.cancel()}</Button
              >
            </div>
          {:else}
            <Button
              icon="logout"
              testid="sessions-revoke-others"
              onclick={() => (confirmingOthers = true)}>{m.sec_others()}</Button
            >
          {/if}
        {/if}
        <p class="hint">{m.sec_sessions_hint()}</p>
      </div>
    </Panel>
  </div>

  <!-- The demo refuses every change of the account's security (Identity's DEMO_POLICY). -->
  <DemoLock inset testid="demo-lock-security">
    <Panel label={m.sec_alerts_title()} testid="security-alerts">
      <Switch
        id="security-new-login-alerts"
        label={m.sec_alerts_label()}
        hint={m.sec_alerts_hint()}
        checked={overview.new_login_alerts}
        states={{ on: m.switch_on(), off: m.switch_off() }}
        disabled={alertsBusy}
        testid="security-alerts-switch"
        onchange={(on) => void setAlerts(on)}
      />
    </Panel>

    <div id="security-passkeys"><PasskeysPanel /></div>
    <div id="security-providers"><LoginLinksPanel /></div>

    <div id="security-tokens">
      <Panel label={m.sec_tokens_title()} testid="security-tokens">
        {#if activeTokens.length === 0}
          <p class="quiet small">{m.sec_tokens_empty()}</p>
        {:else}
          <ul class="tokens">
            {#each activeTokens as token (token.id)}
              <li class="token" data-testid="security-token-{token.id}">
                <div class="what">
                  <span class="device">{token.name}</span>
                  <span class="meta"
                    ><code>{token.prefix}…</code> · {m.sec_token_expires({
                      days: Math.max(0, daysLeft(token.expires_at, now)),
                    })}{#if token.last_used_at}
                      · {m.sec_active({ age: formatAge(token.last_used_at, now) })}{/if}</span
                  >
                </div>
                <div class="act">
                  <Button
                    variant="ghost"
                    icon="trash"
                    pending={working === token.id}
                    testid="security-token-revoke-{token.id}"
                    onclick={() => revokeToken(token)}>{m.sec_token_revoke()}</Button
                  >
                </div>
              </li>
            {/each}
          </ul>
        {/if}
        <p class="hint"><a href="#api-access">{m.sec_tokens_manage()}</a></p>
      </Panel>
    </div>
  </DemoLock>

  <Panel label={m.sec_events_title({ days: EVENT_DAYS })} testid="security-events">
    {#if days.length === 0}
      <p class="quiet small">{m.sec_events_empty()}</p>
    {:else}
      <div class="timeline">
        {#each days as group (group.day)}
          <section class="day">
            <h3 class="day-head">{formatDate(group.events[0]?.at ?? group.day)}</h3>
            <ol>
              {#each group.events as event (event.id)}
                <li
                  class="event tone-{eventTone(event)}"
                  data-testid="security-event-{event.action}"
                >
                  <time class="at" datetime={event.at}>{formatClock(event.at)}</time>
                  <span class="dot" aria-hidden="true"></span>
                  <span class="ev-title">{eventTitle(event)}</span>
                  <span class="ev-meta">{whereText(event.ip_prefix, event.network)}</span>
                </li>
              {/each}
            </ol>
          </section>
        {/each}
      </div>
    {/if}
  </Panel>
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .intro {
    max-width: 720px;
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .quiet {
    color: var(--md-color-ink-muted);
  }

  .small {
    margin: 0;
    font-size: var(--md-font-size-14);
  }

  .hint {
    margin: var(--md-space-3) 0 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-20);
  }

  .hint a {
    color: var(--md-color-ink);
  }

  /* The posture strip: five gauges in a hairline grid, like the dials of an instrument panel.
     Phones get two columns (the last gauge spans both), wide screens one row. */
  .posture {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0;
    margin: 0 var(--md-gutter) var(--md-space-4);
    padding: 0;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    list-style: none;
  }

  .posture li:last-child {
    grid-column: 1 / -1;
  }

  .posture li {
    border-inline-end: var(--md-hairline) solid var(--md-color-line);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .gauge {
    display: grid;
    gap: var(--md-space-1);
    min-height: 96px;
    padding: var(--md-space-3);
    color: inherit;
    text-decoration: none;
  }

  .gauge:hover {
    background: var(--md-color-surface-sunken);
  }

  .gauge:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .g-label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .g-value {
    font-size: var(--md-font-size-16);
    font-weight: var(--md-weight-semibold);
    line-height: var(--md-line-24);
  }

  .g-value.num {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-28);
    font-variant-numeric: tabular-nums slashed-zero;
    line-height: var(--md-line-32);
  }

  .g-value.warn {
    color: var(--md-state-paused);
  }

  .g-meta {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  @media (min-width: 760px) {
    .posture {
      grid-template-columns: repeat(5, minmax(0, 1fr));
    }

    .posture li,
    .posture li:last-child {
      grid-column: auto;
      border-bottom: 0;
    }

    .posture li:last-child {
      border-inline-end: 0;
    }
  }

  /* The session a notification flagged: an alert band with the one action that matters. */
  .flag {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: var(--md-space-3);
    margin: 0 var(--md-gutter) var(--md-space-4);
    padding: var(--md-space-3) var(--md-space-4);
    border-inline-start: 4px solid var(--md-state-error);
    background: var(--md-state-error-bg);
    color: var(--md-color-ink);
  }

  .flag :global(svg) {
    color: var(--md-state-error);
  }

  .flag-text {
    display: grid;
    gap: var(--md-space-1);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .flag-actions {
    display: flex;
    flex-wrap: wrap;
    grid-column: 1 / -1;
    gap: var(--md-space-2);
  }

  .sessions,
  .tokens,
  .timeline ol {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .session,
  .token {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: start;
    gap: var(--md-space-3);
    padding: var(--md-space-3) 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .token {
    grid-template-columns: minmax(0, 1fr);
  }

  .session.current .mark {
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .session.flagged {
    box-shadow: inset 4px 0 0 var(--md-state-error);
    padding-inline-start: var(--md-space-3);
  }

  .mark {
    display: grid;
    place-items: center;
    width: 40px;
    height: 40px;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
  }

  .what {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .device {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--md-space-2);
    font-weight: var(--md-weight-semibold);
    overflow-wrap: anywhere;
  }

  .tag {
    padding: 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-ink);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .meta {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-20);
    overflow-wrap: anywhere;
  }

  .times,
  .meta code {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    font-variant-numeric: tabular-nums;
  }

  .act {
    display: flex;
    flex-wrap: wrap;
    grid-column: 2;
    gap: var(--md-space-2);
  }

  .token .act {
    grid-column: 1;
  }

  @media (min-width: 640px) {
    .session {
      grid-template-columns: auto minmax(0, 1fr) auto;
      align-items: center;
    }

    .token {
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
    }

    .act,
    .token .act {
      grid-column: auto;
      justify-content: flex-end;
    }
  }

  .foot {
    padding-top: var(--md-space-3);
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .confirm-text {
    margin: 0 0 var(--md-space-2);
    font-size: var(--md-font-size-14);
  }

  /* The events: a timeline with the time in mono on the left and a tone dot on the rail. */
  .day + .day {
    margin-top: var(--md-space-4);
  }

  .day-head {
    margin: 0 0 var(--md-space-2);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .event {
    position: relative;
    display: grid;
    grid-template-columns: 3.5em 12px minmax(0, 1fr);
    column-gap: var(--md-space-2);
    padding: var(--md-space-1) 0;
  }

  .event::before {
    content: '';
    position: absolute;
    top: 0;
    bottom: 0;
    left: calc(3.5em + var(--md-space-2) + 5px);
    width: var(--md-hairline);
    background: var(--md-color-line);
  }

  .at {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    font-variant-numeric: tabular-nums;
    line-height: var(--md-line-20);
  }

  .dot {
    position: relative;
    z-index: 1;
    width: 11px;
    height: 11px;
    margin-top: 5px;
    border: 2px solid var(--md-color-surface);
    border-radius: var(--md-radius-round);
    background: var(--md-color-ink-muted);
  }

  .tone-notice .dot {
    background: var(--md-state-paused);
  }

  .tone-alert .dot {
    background: var(--md-state-error);
  }

  .ev-title {
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .tone-alert .ev-title {
    color: var(--md-state-error);
    font-weight: var(--md-weight-semibold);
  }

  .ev-meta {
    grid-column: 3;
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
  }
</style>

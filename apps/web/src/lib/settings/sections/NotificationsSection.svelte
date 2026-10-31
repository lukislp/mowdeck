<script lang="ts">
  import { Button, Field, Icon, Panel, ProblemNotice, Readout, Switch } from '@mowdeck/ui';

  import { fleetApi, notificationsApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import DemoLock from '#lib/demo/DemoLock.svelte';
  import type { components } from '#lib/generated/notifications.js';
  import { formatDateTime, problemMessage } from '#lib/i18n.js';
  import NativeAppPanel from '#lib/native/NativeAppPanel.svelte';
  import { nativeShell } from '#lib/native/runtime.js';
  import {
    channelHint,
    channelLabel,
    kindHint,
    kindLabel,
    USER_KINDS,
  } from '#lib/notifications/kinds.js';
  import { inbox, pushDevice } from '#lib/notifications/runtime.js';
  import { m } from '#lib/paraglide/messages.js';
  import { getLocale } from '#lib/paraglide/runtime.js';
  import {
    describeAgent,
    detectEnvironment,
    pushSupport,
    PushError,
    type PushSupport,
  } from '#lib/push/device.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  type Preferences = components['schemas']['PreferencesOut'];
  type Subscription = components['schemas']['PushSubscriptionOut'];
  type Ntfy = components['schemas']['NtfyOut'];

  const switchStates = $derived({ on: m.switch_on(), off: m.switch_off() });
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

  // --- preferences ------------------------------------------------------------------------------

  let prefs = $state<Preferences | null>(null);
  let loadProblem = $state<ApiProblem | null>(null);
  let kinds = $state<Record<string, boolean>>({});
  let allMowers = $state(false);
  let mowerIds = $state<Record<string, boolean>>({});
  let quiet = $state(false);
  let quietStart = $state('22:00');
  let quietEnd = $state('07:00');
  let channels = $state<Record<string, boolean>>({});
  let mowers = $state<{ id: string; name: string }[]>([]);
  let saving = $state(false);
  let saveProblem = $state<ApiProblem | null>(null);

  const offeredKinds = $derived(
    USER_KINDS.filter(
      (k) => (prefs?.available_kinds as string[] | undefined)?.includes(k) ?? false,
    ),
  );

  function show(found: Preferences): void {
    prefs = found;
    kinds = Object.fromEntries(
      found.available_kinds.map((k) => [k, found.enabled_kinds.includes(k)]),
    );
    allMowers = found.all_mowers;
    mowerIds = Object.fromEntries(found.mower_ids.map((id) => [id, true]));
    quiet = found.quiet_hours !== null;
    quietStart = found.quiet_hours?.start ?? '22:00';
    quietEnd = found.quiet_hours?.end ?? '07:00';
    channels = Object.fromEntries(
      found.available_channels.map((c) => [c, found.channels.includes(c)]),
    );
  }

  async function savePreferences(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!prefs) return;
    saving = true;
    saveProblem = null;
    const language = prefs.languages.includes(getLocale()) ? getLocale() : prefs.language;
    try {
      const saved = await unwrap(
        notificationsApi.PUT('/api/v1/notifications/preferences', {
          body: {
            enabled_kinds: Object.entries(kinds)
              .filter(([, on]) => on)
              .map(([kind]) => kind as components['schemas']['NotificationKind']),
            all_mowers: allMowers,
            mower_ids: allMowers
              ? []
              : Object.entries(mowerIds)
                  .filter(([, on]) => on)
                  .map(([id]) => id),
            quiet_hours: quiet ? { start: quietStart, end: quietEnd } : null,
            timezone,
            language,
            channels: Object.entries(channels)
              .filter(([, on]) => on)
              .map(([channel]) => channel),
          },
        }),
      );
      show(saved);
      toasts.push('ok', m.saved());
    } catch (error) {
      saveProblem = asProblem(error);
    } finally {
      saving = false;
    }
  }

  // --- Web Push ---------------------------------------------------------------------------------

  let support = $state<PushSupport>('unsupported');
  let permission = $state<NotificationPermission | 'unknown'>('unknown');
  let subscriptions = $state<Subscription[]>([]);
  let thisDevice = $state<string | null>(null);
  let pushBusy = $state(false);
  let pushProblem = $state<ApiProblem | null>(null);
  let pushFailure = $state<'denied' | 'no-key' | 'no-subscription' | null>(null);
  let removing = $state<string | null>(null);

  const deviceListed = $derived(
    thisDevice !== null && subscriptions.some((s) => s.id === thisDevice),
  );

  async function loadSubscriptions(): Promise<void> {
    const list = await unwrap(notificationsApi.GET('/api/v1/notifications/push-subscriptions'));
    subscriptions = list.items;
    thisDevice = pushDevice.deviceId();
    if (thisDevice && support === 'supported' && !(await pushDevice.subscribed())) {
      thisDevice = null; // the browser dropped the subscription (cleared site data)
    }
  }

  async function enablePush(): Promise<void> {
    pushBusy = true;
    pushProblem = null;
    pushFailure = null;
    try {
      await pushDevice.enable();
      permission = Notification.permission;
      await loadSubscriptions();
      toasts.push('ok', m.notif_push_enabled());
    } catch (error) {
      permission = typeof Notification === 'undefined' ? 'unknown' : Notification.permission;
      if (error instanceof PushError) pushFailure = error.reason;
      else pushProblem = asProblem(error);
    } finally {
      pushBusy = false;
    }
  }

  async function removeSubscription(id: string): Promise<void> {
    removing = id;
    pushProblem = null;
    try {
      await pushDevice.remove(id);
      await loadSubscriptions();
    } catch (error) {
      pushProblem = asProblem(error);
    } finally {
      removing = null;
    }
  }

  // --- ntfy -------------------------------------------------------------------------------------

  let ntfy = $state<Ntfy | null>(null);
  let ntfyServer = $state('https://ntfy.sh');
  let ntfyTopic = $state('');
  let ntfyToken = $state('');
  let ntfyBusy = $state(false);
  let ntfyProblem = $state<ApiProblem | null>(null);

  function showNtfy(found: Ntfy | null): void {
    ntfy = found;
    ntfyServer = found?.server_url ?? 'https://ntfy.sh';
    ntfyTopic = found?.topic ?? '';
    ntfyToken = '';
  }

  async function saveNtfy(clearToken = false): Promise<void> {
    ntfyBusy = true;
    ntfyProblem = null;
    try {
      const token = clearToken ? '' : ntfyToken.trim() || null;
      showNtfy(
        await unwrap(
          notificationsApi.PUT('/api/v1/notifications/ntfy', {
            body: { server_url: ntfyServer.trim(), topic: ntfyTopic.trim(), token },
          }),
        ),
      );
      toasts.push('ok', m.saved());
      void nativeShell.refreshNtfy();
    } catch (error) {
      ntfyProblem = asProblem(error);
    } finally {
      ntfyBusy = false;
    }
  }

  async function removeNtfy(): Promise<void> {
    ntfyBusy = true;
    ntfyProblem = null;
    try {
      await unwrap(notificationsApi.DELETE('/api/v1/notifications/ntfy'));
      showNtfy(null);
      toasts.push('ok', m.notif_ntfy_removed());
      void nativeShell.refreshNtfy();
    } catch (error) {
      ntfyProblem = asProblem(error);
    } finally {
      ntfyBusy = false;
    }
  }

  // --- test -------------------------------------------------------------------------------------

  let testBusy = $state(false);
  let testSent = $state<string[] | null>(null);
  let testProblem = $state<ApiProblem | null>(null);
  let cooldownUntil = $state(0);
  let now = $state(Date.now());
  const cooldown = $derived(Math.max(0, Math.ceil((cooldownUntil - now) / 1000)));

  $effect(() => {
    if (cooldownUntil <= Date.now()) return;
    const timer = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(timer);
  });

  async function sendTest(): Promise<void> {
    testBusy = true;
    testProblem = null;
    testSent = null;
    try {
      const sent = await unwrap(notificationsApi.POST('/api/v1/notifications/test'));
      testSent = sent.channels;
      void inbox.refresh();
    } catch (error) {
      const problem = asProblem(error);
      if (problem.status === 429) {
        now = Date.now();
        cooldownUntil = now + (problem.retryAfter ?? 60) * 1000;
      } else {
        testProblem = problem;
      }
    } finally {
      testBusy = false;
    }
  }

  // --- load -------------------------------------------------------------------------------------

  $effect(() => {
    support = pushSupport(detectEnvironment());
    permission = typeof Notification === 'undefined' ? 'unknown' : Notification.permission;
    void (async () => {
      try {
        show(await unwrap(notificationsApi.GET('/api/v1/notifications/preferences')));
      } catch (error) {
        loadProblem = asProblem(error);
        return;
      }
      void unwrap(fleetApi.GET('/api/v1/fleet/mowers'))
        .then((list) => (mowers = list.items.map((x) => ({ id: x.id, name: x.name }))))
        .catch(() => undefined);
      void loadSubscriptions().catch((error: unknown) => (pushProblem = asProblem(error)));
      void unwrap(notificationsApi.GET('/api/v1/notifications/ntfy'))
        .then(showNtfy)
        .catch((error: unknown) => {
          const problem = asProblem(error);
          if (problem.status !== 404) ntfyProblem = problem;
        });
    })();
  });

  function permissionLabel(value: NotificationPermission | 'unknown'): string {
    switch (value) {
      case 'granted':
        return m.notif_permission_granted();
      case 'denied':
        return m.notif_permission_denied();
      case 'default':
        return m.notif_permission_default();
      case 'unknown':
        return m.no_data();
    }
  }

  function supportNotice(value: PushSupport): string | null {
    switch (value) {
      case 'insecure':
        return m.notif_push_insecure();
      case 'ios-install':
        return m.notif_push_ios();
      case 'unsupported':
        return m.notif_push_unsupported();
      case 'supported':
        return null;
    }
  }

  function failureNotice(value: 'denied' | 'no-key' | 'no-subscription'): string {
    switch (value) {
      case 'denied':
        return m.notif_push_denied();
      case 'no-key':
        return m.notif_push_no_key();
      case 'no-subscription':
        return m.notif_push_failed();
    }
  }
</script>

{#if loadProblem}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(loadProblem)}
      detail={loadProblem.detail}
      reference={loadProblem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if !prefs}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{:else}
  <div class="columns">
    <form class="column" onsubmit={savePreferences} novalidate data-testid="notif-preferences">
      {#if !prefs.stored}
        <div class="pad">
          <ProblemNotice tone="info" title={m.notif_not_stored()} />
        </div>
      {/if}

      <Panel label={m.notif_events()} testid="notif-kinds">
        <div class="list">
          {#each offeredKinds as kind (kind)}
            <Switch
              id="kind-{kind}"
              testid="kind-{kind}"
              label={kindLabel(kind)}
              hint={kindHint(kind)}
              states={switchStates}
              bind:checked={kinds[kind]}
            />
          {/each}
        </div>
      </Panel>

      <Panel label={m.notif_mowers()} testid="notif-mowers">
        <div class="list">
          <Switch
            id="all-mowers"
            testid="all-mowers"
            label={m.notif_all_mowers()}
            hint={m.notif_all_mowers_hint()}
            states={switchStates}
            bind:checked={allMowers}
          />
          {#if !allMowers}
            <div class="sub">
              {#each mowers as mower (mower.id)}
                <Switch
                  id="mower-{mower.id}"
                  testid="mower-{mower.id}"
                  label={mower.name}
                  states={switchStates}
                  bind:checked={mowerIds[mower.id]}
                />
              {:else}
                <p class="quiet">{m.notif_no_mowers()}</p>
              {/each}
            </div>
          {/if}
        </div>
      </Panel>

      <Panel label={m.notif_quiet_hours()} testid="notif-quiet">
        <div class="list">
          <Switch
            id="quiet-hours"
            testid="quiet-hours"
            label={m.notif_quiet_enable()}
            hint={m.notif_quiet_hint()}
            states={switchStates}
            bind:checked={quiet}
          />
          {#if quiet}
            <div class="times">
              <Field
                id="quiet-start"
                type="time"
                label={m.notif_quiet_start()}
                mono
                bind:value={quietStart}
              />
              <Field
                id="quiet-end"
                type="time"
                label={m.notif_quiet_end()}
                mono
                bind:value={quietEnd}
              />
            </div>
          {/if}
          <Readout
            label={m.notif_timezone()}
            value={timezone}
            size="inline"
            emptyText={m.no_data()}
            testid="notif-timezone"
          />
        </div>
      </Panel>

      <Panel label={m.notif_channels()} testid="notif-channels">
        <div class="list">
          {#each prefs.available_channels as channel (channel)}
            <Switch
              id="channel-{channel}"
              testid="channel-{channel}"
              label={channelLabel(channel)}
              hint={channelHint(channel)}
              states={switchStates}
              bind:checked={channels[channel]}
            />
          {/each}
        </div>
      </Panel>

      <div class="actions">
        {#if saveProblem}
          <ProblemNotice
            title={problemMessage(saveProblem)}
            detail={saveProblem.detail}
            reference={saveProblem.correlationId}
            referenceLabel={m.problem_reference()}
          />
        {/if}
        <Button type="submit" variant="primary" pending={saving} testid="notif-save"
          >{m.save()}</Button
        >
      </div>
    </form>

    <div class="column">
      <!-- The demo sends nothing outside the app (Notifications' DEMO_POLICY): no push, ntfy or
           test message; the preferences on the left and the inbox work. -->
      <DemoLock inset testid="demo-lock-channels">
        {#if nativeShell.active}
          <!-- Inside the iOS/Android app: Apple Push, local notifications, ntfy instead of Web Push. -->
          <NativeAppPanel
            apnsOnServer={prefs.available_channels.includes('apns')}
            ntfyTarget={ntfy}
          />
        {:else if prefs.available_channels.includes('webpush')}
          <Panel label={m.notif_push_title()} testid="push-panel">
            <div class="stack">
              <div class="readouts">
                <Readout
                  label={m.notif_push_permission()}
                  value={permissionLabel(permission)}
                  size="inline"
                  emptyText={m.no_data()}
                  testid="push-permission"
                />
                <Readout
                  label={m.notif_push_device()}
                  value={deviceListed ? m.notif_push_device_on() : m.notif_push_device_off()}
                  size="inline"
                  emptyText={m.no_data()}
                  testid="push-device"
                />
              </div>
              {#if supportNotice(support)}
                <ProblemNotice tone="info" title={supportNotice(support) ?? ''} />
              {:else}
                {#if pushFailure}
                  <ProblemNotice title={failureNotice(pushFailure)} />
                {:else if permission === 'denied' && !deviceListed}
                  <ProblemNotice tone="info" title={m.notif_push_denied()} />
                {/if}
                {#if !deviceListed}
                  <div>
                    <Button
                      variant="primary"
                      icon="bell"
                      pending={pushBusy}
                      disabled={permission === 'denied'}
                      testid="push-enable"
                      onclick={enablePush}>{m.notif_push_enable()}</Button
                    >
                  </div>
                  <p class="quiet small">{m.notif_push_enable_hint()}</p>
                {/if}
              {/if}
              {#if pushProblem}
                <ProblemNotice
                  title={problemMessage(pushProblem)}
                  detail={pushProblem.detail}
                  reference={pushProblem.correlationId}
                  referenceLabel={m.problem_reference()}
                />
              {/if}
              <span class="label">{m.notif_push_devices()}</span>
              <ul class="devices" data-testid="push-subscriptions">
                {#each subscriptions as item (item.id)}
                  <li data-testid="push-subscription">
                    <Icon name="phone" size={20} />
                    <span class="device">
                      <span class="name">
                        {describeAgent(item.user_agent) ?? m.notif_push_unknown_device()}
                        {#if item.id === thisDevice}
                          <span class="this" data-testid="push-this-device"
                            >{m.notif_push_this()}</span
                          >
                        {/if}
                      </span>
                      <span class="meta"
                        >{item.endpoint_host} · {formatDateTime(item.created_at)}</span
                      >
                    </span>
                    <Button
                      variant="ghost"
                      icon="trash"
                      label={m.remove()}
                      pending={removing === item.id}
                      testid="push-remove-{item.id}"
                      onclick={() => removeSubscription(item.id)}
                    />
                  </li>
                {:else}
                  <li class="none">{m.notif_push_none()}</li>
                {/each}
              </ul>
            </div>
          </Panel>
        {/if}

        {#if prefs.available_channels.includes('ntfy')}
          <Panel label={m.notif_ntfy_title()} testid="ntfy-panel">
            <form
              class="stack"
              novalidate
              onsubmit={(event) => {
                event.preventDefault();
                void saveNtfy();
              }}
            >
              <p class="quiet small">{m.notif_ntfy_hint()}</p>
              <Readout
                label={m.notif_ntfy_status()}
                value={ntfy ? `${ntfy.server_url}/${ntfy.topic}` : m.notif_ntfy_none()}
                size="inline"
                emptyText={m.no_data()}
                testid="ntfy-status"
              />
              <Field
                id="ntfy-server"
                type="url"
                label={m.notif_ntfy_server()}
                mono
                bind:value={ntfyServer}
                error={ntfyProblem?.fieldError('server_url') ?? null}
              />
              <Field
                id="ntfy-topic"
                label={m.notif_ntfy_topic()}
                hint={m.notif_ntfy_topic_hint()}
                mono
                maxlength={64}
                bind:value={ntfyTopic}
                error={ntfyProblem?.fieldError('topic') ?? null}
              />
              <div class="secret">
                {#if ntfy?.has_token}
                  <span class="set" data-testid="ntfy-token-set">
                    <Icon name="check" size={14} />{m.conn_secret_set()}
                  </span>
                {/if}
                <Field
                  id="ntfy-token"
                  type="password"
                  label={m.notif_ntfy_token()}
                  autocomplete="new-password"
                  mono
                  hint={ntfy?.has_token ? m.conn_secret_keep_hint() : m.notif_ntfy_token_hint()}
                  bind:value={ntfyToken}
                />
              </div>
              {#if ntfyProblem}
                <ProblemNotice
                  title={problemMessage(ntfyProblem)}
                  detail={ntfyProblem.detail}
                  reference={ntfyProblem.correlationId}
                  referenceLabel={m.problem_reference()}
                />
              {/if}
              <div class="row">
                <Button
                  type="submit"
                  variant="primary"
                  pending={ntfyBusy}
                  disabled={!ntfyServer.trim() || !ntfyTopic.trim()}
                  testid="ntfy-save">{m.save()}</Button
                >
                {#if ntfy?.has_token}
                  <Button variant="ghost" pending={ntfyBusy} onclick={() => saveNtfy(true)}
                    >{m.notif_ntfy_clear_token()}</Button
                  >
                {/if}
                {#if ntfy}
                  <Button
                    variant="danger"
                    icon="trash"
                    pending={ntfyBusy}
                    testid="ntfy-remove"
                    onclick={removeNtfy}>{m.remove()}</Button
                  >
                {/if}
              </div>
            </form>
          </Panel>
        {/if}

        <Panel label={m.notif_test_title()} testid="test-panel">
          <div class="stack">
            <p class="quiet small">{m.notif_test_hint()}</p>
            <div>
              <Button
                icon="send"
                pending={testBusy}
                disabled={cooldown > 0}
                testid="notif-test"
                onclick={sendTest}>{m.notif_test_send()}</Button
              >
            </div>
            {#if cooldown > 0}
              <p class="status" data-testid="notif-test-cooldown" role="status">
                <Icon name="pause" size={16} />{m.notif_test_cooldown({ seconds: cooldown })}
              </p>
            {:else if testSent}
              <p class="status" data-testid="notif-test-result" role="status">
                <Icon name="check" size={16} />{testSent.length
                  ? m.notif_test_sent({ channels: testSent.map(channelLabel).join(', ') })
                  : m.notif_test_inbox_only()}
              </p>
            {/if}
            {#if testProblem}
              <ProblemNotice
                title={problemMessage(testProblem)}
                detail={testProblem.detail}
                reference={testProblem.correlationId}
                referenceLabel={m.problem_reference()}
              />
            {/if}
          </div>
        </Panel>
      </DemoLock>
    </div>
  </div>
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .quiet {
    margin: 0;
    color: var(--md-color-ink-muted);
  }

  .small {
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .columns {
    display: grid;
    max-width: 720px;
  }

  .column {
    min-width: 0;
  }

  .list {
    display: flex;
    flex-direction: column;
  }

  .list > :global(*) + :global(*) {
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .sub {
    padding-inline-start: var(--md-space-4);
    border-inline-start: 3px solid var(--md-color-line);
  }

  .times {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--md-space-3);
    padding: var(--md-space-3) 0;
  }

  .list > :global(.md-readout) {
    padding-top: var(--md-space-3);
  }

  .stack {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-3);
  }

  .readouts {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--md-space-3);
    padding: var(--md-space-3);
    background: var(--md-color-surface-sunken);
  }

  .label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .devices {
    margin: 0;
    padding: 0;
    list-style: none;
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .devices li {
    display: flex;
    align-items: center;
    gap: var(--md-space-3);
    min-height: var(--md-target);
    padding: var(--md-space-1) 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .devices li.none {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .device {
    display: flex;
    flex: 1;
    flex-direction: column;
    min-width: 0;
  }

  .name {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2);
    font-weight: var(--md-weight-medium);
  }

  .meta {
    overflow: hidden;
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .this,
  .set {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    padding: 0 var(--md-space-1);
    border: var(--md-hairline) solid var(--md-color-ink);
    border-radius: var(--md-radius-1);
    color: var(--md-color-ink);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-medium);
    line-height: var(--md-line-16);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .secret {
    position: relative;
  }

  .set {
    position: absolute;
    top: 0;
    right: 0;
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .status {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    margin: 0;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-14);
  }

  .actions {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--md-space-3);
    padding: var(--md-space-2) var(--md-gutter) var(--md-space-6);
  }

  @media (min-width: 1024px) {
    .columns {
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      max-width: 1200px;
    }

    .column + .column {
      border-inline-start: var(--md-hairline) solid var(--md-color-line);
    }
  }
</style>

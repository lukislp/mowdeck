<!--
  RTK base stations of the account (ADR 0047): register one from a linked vendor account's
  device list (or the simulator's), assign and unassign the mowers that use it, see its state as
  a linked mower reports it (live over the socket), and check and install its firmware through a
  linked mower. Changes need `can_manage_mowers` on the account; Fleet decides again on every
  request, and the firmware update follows the controller's rules (the relaying mower rests).
-->
<script lang="ts">
  import { Button, Field, Icon, ProblemNotice, Select, Sheet, TopBar } from '@mowdeck/ui';

  import { fleetApi, telemetryApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { session } from '#lib/auth/session.svelte.js';
  import { clock } from '#lib/clock.svelte.js';
  import BaseStationFirmware from '#lib/components/BaseStationFirmware.svelte';
  import BaseStationStatus from '#lib/components/BaseStationStatus.svelte';
  import FleetTabs from '#lib/components/FleetTabs.svelte';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import { loadKinds } from '#lib/connection/kinds.js';
  import { baseStationProblemText } from '#lib/fleet/base-station-labels.js';
  import {
    baseStationDevices,
    baseUpdateBlocker,
    canServe,
    checkBaseStationFirmware,
    commandSettled,
    deleteBaseStation,
    getCommand,
    listBaseStations,
    newerInfo,
    registerBaseStation,
    renameBaseStation,
    reportOf,
    setMowerBaseStation,
    updateBaseStationFirmware,
    type BaseStation,
    type BaseStationIn,
    type FirmwareInfo,
  } from '#lib/fleet/base-stations.js';
  import type { components } from '#lib/generated/fleet.js';
  import { problemMessage } from '#lib/i18n.js';
  import type { LinkedAccount, LinkedDevice } from '#lib/linked/accounts.js';
  import { listDevices, listLinkedAccounts } from '#lib/linked/api.js';
  import { permissions, realtime } from '#lib/live.svelte.js';
  import { m } from '#lib/paraglide/messages.js';
  import { accountPlan } from '#lib/plan/plan.svelte.js';
  import type { CommandOutcome } from '#lib/stores/commands.svelte.js';
  import { mowers as states } from '#lib/stores/mowers.svelte.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  type Mower = components['schemas']['MowerOut'];

  let bases = $state<BaseStation[] | null>(null);
  let mowers = $state<Mower[]>([]);
  let problem = $state<ApiProblem | null>(null);
  let canManage = $state(false);
  /** `base_station_firmware_update` of each linked mower's connection. */
  let relayCapable = $state<Record<string, boolean>>({});
  let checking = $state<string | null>(null);
  let checked = $state<Record<string, FirmwareInfo>>({});
  let requests = $state<Record<string, { status: CommandOutcome; reason: string | null }>>({});
  let renaming = $state<string | null>(null);
  let renameValue = $state('');
  let confirmDelete = $state<string | null>(null);
  let linkChoice = $state<Record<string, string>>({});
  let busy = $state<string | null>(null);

  // Registering: the base stations of the linked accounts' device lists, and the simulator's.
  let registerOpen = $state(false);
  let candidates = $state<{ account: LinkedAccount; devices: LinkedDevice[] }[] | null>(null);
  let simulatedAllowed = $state(false);
  let registerProblem = $state<string | null>(null);

  const accountId = $derived(session.me?.account_id ?? null);
  const mowerById = $derived(new Map(mowers.map((x) => [x.id, x] as const)));
  const linkedIds = $derived([...new Set((bases ?? []).flatMap((b) => b.mower_ids))]);
  const baseName = $derived(new Map((bases ?? []).map((b) => [b.id, b.name] as const)));

  async function load(): Promise<void> {
    try {
      const [list, all] = await Promise.all([
        listBaseStations(),
        unwrap(fleetApi.GET('/api/v1/fleet/mowers')),
      ]);
      bases = list;
      mowers = all.items;
      problem = null;
    } catch (error) {
      problem = asProblem(error);
      return;
    }
    for (const mowerId of linkedIds) {
      const path = { params: { path: { mower_id: mowerId } } };
      if (!states.has(mowerId)) {
        void unwrap(telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/state', path))
          .then((snap) => states.upsert(mowerId, snap.state, 'rest'))
          .catch(() => undefined);
      }
      if (!(mowerId in relayCapable)) {
        void unwrap(telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/capabilities', path))
          .then(
            (snap) =>
              (relayCapable = {
                ...relayCapable,
                [mowerId]: snap.capabilities.base_station_firmware_update === true,
              }),
          )
          .catch(() => undefined);
      }
    }
  }

  $effect(() => {
    void load();
  });
  $effect(() => {
    const account = accountId;
    void permissions.version;
    if (!account) return;
    void permissions
      .checkObjects([`account:${account}`], ['can_manage_mowers'])
      .then((found) => (canManage = found[`account:${account}`]?.can_manage_mowers ?? false))
      .catch(() => (canManage = false));
  });
  $effect(() => realtime.watch(linkedIds));
  $effect(() => clock.hold(5_000));

  // --- per base station ------------------------------------------------------------------------

  function nameOf(mowerId: string): string {
    return mowerById.get(mowerId)?.name ?? mowerId.slice(0, 8);
  }

  function linkedMowers(base: BaseStation): Mower[] {
    return base.mower_ids
      .map((id) => mowerById.get(id))
      .filter((x): x is Mower => !!x)
      .sort((a, b) => a.name.localeCompare(b.name));
  }

  function assignable(base: BaseStation): Mower[] {
    return mowers
      .filter((x) => x.base_station_id !== base.id && canServe(base, x))
      .sort((a, b) => a.name.localeCompare(b.name));
  }

  function report(base: BaseStation) {
    return reportOf(base, (mowerId) => states.get(mowerId)?.state);
  }

  function relaysOf(base: BaseStation, info: FirmwareInfo | null) {
    return linkedMowers(base).map((mower) => {
      const snap = states.get(mower.id);
      return {
        id: mower.id,
        name: mower.name,
        blocker: baseUpdateBlocker({
          allowed: true,
          features: null,
          restricted: false,
          relay: {
            activity: snap?.state.activity ?? null,
            offline: snap === undefined || states.isOffline(mower.id, clock.now),
            capable: relayCapable[mower.id] ?? false,
            driveSession: realtime.drive?.mowerId === mower.id,
          },
          info,
        }),
      };
    });
  }

  /** Rights and plan decide for every mower alike. */
  const accountBlocker = $derived(
    baseUpdateBlocker({
      allowed: canManage,
      features: accountPlan.current?.features ?? null,
      restricted: accountPlan.restricted,
      relay: { activity: 'idle', offline: false, capable: true, driveSession: false },
      info: { update_state: 'available', available: 'x' },
    }),
  );

  async function link(base: BaseStation): Promise<void> {
    const mowerId = linkChoice[base.id] ?? assignable(base)[0]?.id;
    if (!mowerId) return;
    busy = `link:${base.id}`;
    try {
      await setMowerBaseStation(mowerId, base.id);
      toasts.push('ok', m.bs_linked({ mower: nameOf(mowerId), base: base.name }));
      linkChoice = { ...linkChoice, [base.id]: '' };
      await load();
    } catch (error) {
      const p = asProblem(error);
      toasts.push('error', baseStationProblemText(p), undefined, p.correlationId ?? undefined);
    } finally {
      busy = null;
    }
  }

  async function unlink(mowerId: string): Promise<void> {
    busy = `unlink:${mowerId}`;
    try {
      await setMowerBaseStation(mowerId, null);
      toasts.push('ok', m.bs_unlinked({ mower: nameOf(mowerId) }));
      await load();
    } catch (error) {
      const p = asProblem(error);
      toasts.push('error', baseStationProblemText(p), undefined, p.correlationId ?? undefined);
    } finally {
      busy = null;
    }
  }

  async function saveName(base: BaseStation): Promise<void> {
    const name = renameValue.trim();
    if (!name || name === base.name) {
      renaming = null;
      return;
    }
    busy = `rename:${base.id}`;
    try {
      await renameBaseStation(base.id, name);
      toasts.push('ok', m.bs_renamed());
      renaming = null;
      await load();
    } catch (error) {
      const p = asProblem(error);
      toasts.push('error', problemMessage(p), undefined, p.correlationId ?? undefined);
    } finally {
      busy = null;
    }
  }

  async function remove(base: BaseStation): Promise<void> {
    busy = `delete:${base.id}`;
    try {
      await deleteBaseStation(base.id);
      toasts.push('ok', m.bs_deleted());
      confirmDelete = null;
      await load();
    } catch (error) {
      const p = asProblem(error);
      toasts.push('error', problemMessage(p), undefined, p.correlationId ?? undefined);
    } finally {
      busy = null;
    }
  }

  async function check(base: BaseStation): Promise<void> {
    if (checking) return;
    checking = base.id;
    try {
      const info = await checkBaseStationFirmware(base.id);
      checked = { ...checked, [base.id]: info };
      toasts.push(
        'ok',
        info.available && info.update_state === 'available'
          ? m.bs_fw_check_available({ version: info.available })
          : m.bs_fw_check_none(),
      );
    } catch (error) {
      const p = asProblem(error);
      toasts.push(
        'error',
        p.status === 503 || p.status === 504 ? m.bs_fw_check_failed() : baseStationProblemText(p),
        p.detail ?? undefined,
        p.correlationId ?? undefined,
      );
    } finally {
      checking = null;
    }
  }

  async function install(base: BaseStation, version: string, mowerId: string): Promise<void> {
    let commandId: string;
    try {
      commandId = await updateBaseStationFirmware(base.id, version, mowerId);
    } catch (error) {
      const p = asProblem(error);
      toasts.push(
        'error',
        baseStationProblemText(p),
        p.detail ?? undefined,
        p.correlationId ?? undefined,
      );
      return;
    }
    requests = { ...requests, [base.id]: { status: 'pending', reason: null } };
    toasts.push('ok', m.bs_fw_started());
    // Follow the command record until the mower answered (the state shows the progress).
    const deadline = Date.now() + 90_000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 2_000));
      try {
        const view = await getCommand(commandId);
        const status = (
          ['accepted', 'executed', 'rejected', 'failed', 'expired'].includes(view.status)
            ? view.status
            : 'pending'
        ) as CommandOutcome;
        requests = { ...requests, [base.id]: { status, reason: view.reason ?? null } };
        if (commandSettled(view)) return;
      } catch {
        // Try again on the next tick.
      }
    }
  }

  // --- registering -----------------------------------------------------------------------------

  async function openRegister(): Promise<void> {
    registerOpen = true;
    registerProblem = null;
    candidates = null;
    void loadKinds()
      .then((kinds) => (simulatedAllowed = kinds.some((k) => k.kind === 'simulated')))
      .catch(() => (simulatedAllowed = false));
    try {
      const accounts = await listLinkedAccounts();
      candidates = await Promise.all(
        accounts.map(async (account) => {
          try {
            return { account, devices: baseStationDevices((await listDevices(account.id)).items) };
          } catch {
            return { account, devices: [] };
          }
        }),
      );
    } catch {
      candidates = [];
    }
  }

  async function register(body: BaseStationIn, name: string): Promise<void> {
    busy = 'register';
    registerProblem = null;
    try {
      const created = await registerBaseStation(body);
      toasts.push('ok', m.bs_registered({ name: created.name || name }));
      registerOpen = false;
      await load();
    } catch (error) {
      registerProblem = baseStationProblemText(asProblem(error));
    } finally {
      busy = null;
    }
  }

  const deviceCount = $derived((candidates ?? []).reduce((sum, c) => sum + c.devices.length, 0));
</script>

<svelte:head><title>{m.bs_title()} · {m.app_name()}</title></svelte:head>

<TopBar title={m.bs_title()} meta={bases ? String(bases.length) : undefined}>
  {#snippet actions()}
    {#if canManage}
      <Button icon="plus" label={m.bs_add()} testid="bs-add" onclick={() => void openRegister()} />
    {/if}
  {/snippet}
</TopBar>
<FleetTabs />

{#if problem}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if !bases}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{:else if bases.length === 0}
  <section class="empty" data-testid="bs-empty">
    <span class="mark" aria-hidden="true"><Icon name="signal" size={28} /></span>
    <h2>{m.bs_empty_title()}</h2>
    <p class="quiet">{m.bs_empty_body()}</p>
    {#if canManage}
      <Button
        variant="primary"
        icon="plus"
        testid="bs-add-empty"
        onclick={() => void openRegister()}>{m.bs_add()}</Button
      >
    {:else}
      <p class="quiet small">{m.bs_no_permission()}</p>
    {/if}
  </section>
{:else}
  {#if !canManage}
    <p class="pad quiet small" data-testid="bs-read-only">{m.bs_no_permission()}</p>
  {/if}
  <ul class="bases" data-testid="bs-list">
    {#each bases as base (base.id)}
      {@const found = report(base)}
      {@const linked = linkedMowers(base)}
      {@const free = assignable(base)}
      {@const info = newerInfo(found?.state.firmware_info, checked[base.id])}
      <li class="base" data-testid="bs-{base.id}" data-name={base.name}>
        <header class="head">
          <span class="glyph" aria-hidden="true"><Icon name="signal" size={20} /></span>
          <div class="titles">
            {#if renaming === base.id}
              <form
                class="rename"
                onsubmit={(event) => {
                  event.preventDefault();
                  void saveName(base);
                }}
              >
                <Field
                  id="bs-name-{base.id}"
                  label={m.bs_name()}
                  maxlength={100}
                  bind:value={renameValue}
                />
                <Button
                  type="submit"
                  variant="primary"
                  pending={busy === `rename:${base.id}`}
                  testid="bs-rename-save">{m.save()}</Button
                >
                <Button variant="ghost" onclick={() => (renaming = null)}>{m.cancel()}</Button>
              </form>
            {:else}
              <h2 data-testid="bs-name">{base.name}</h2>
            {/if}
            <span class="meta mono">
              {[
                base.source === 'cloud' ? m.bs_source_cloud() : m.bs_source_simulated(),
                base.device_name,
                base.model,
              ]
                .filter(Boolean)
                .join(' · ')}
            </span>
          </div>
          {#if canManage && renaming !== base.id}
            <div class="tools">
              <Button
                variant="ghost"
                icon="edit"
                label={m.bs_rename()}
                testid="bs-rename"
                onclick={() => {
                  renaming = base.id;
                  renameValue = base.name;
                }}
              />
              <Button
                variant="ghost"
                icon="trash"
                label={m.bs_delete()}
                testid="bs-delete"
                onclick={() => (confirmDelete = base.id)}
              />
            </div>
          {/if}
        </header>

        <section class="block" aria-label={m.bs_state()}>
          <h3>{m.bs_state()}</h3>
          <BaseStationStatus
            state={found?.state ?? null}
            mowerName={found ? nameOf(found.mowerId) : (linked[0]?.name ?? null)}
            now={clock.now}
          />
        </section>

        <section class="block" aria-label={m.bs_mowers()}>
          <h3>{m.bs_mowers()}</h3>
          {#if linked.length}
            <ul class="chips" data-testid="bs-mowers">
              {#each linked as mower (mower.id)}
                <li class="chip" data-mower={mower.id}>
                  <a href="/mowers/{mower.id}">{mower.name}</a>
                  {#if canManage}
                    <button
                      type="button"
                      class="chip-x"
                      aria-label={m.bs_unlink({ mower: mower.name })}
                      data-testid="bs-unlink"
                      disabled={busy === `unlink:${mower.id}`}
                      onclick={() => void unlink(mower.id)}
                    >
                      <Icon name="close" size={16} />
                    </button>
                  {/if}
                </li>
              {/each}
            </ul>
          {:else}
            <p class="quiet small">{m.bs_mowers_none()}</p>
          {/if}
          {#if canManage}
            <div class="assign" data-testid="bs-assign">
              {#if free.length}
                <Select
                  id="bs-link-{base.id}"
                  label={m.bs_link_choose()}
                  options={free.map((x) => ({
                    value: x.id,
                    label: x.base_station_id
                      ? `${x.name} (${m.bs_link_elsewhere({ name: baseName.get(x.base_station_id) ?? '…' })})`
                      : x.name,
                  }))}
                  bind:value={
                    () => linkChoice[base.id] || free[0]?.id || '',
                    (value) => (linkChoice = { ...linkChoice, [base.id]: value })
                  }
                />
                <Button
                  icon="link"
                  pending={busy === `link:${base.id}`}
                  testid="bs-link"
                  onclick={() => void link(base)}>{m.bs_link_mower()}</Button
                >
              {:else}
                <p class="quiet small">{m.bs_link_choose_none()}</p>
              {/if}
              <p class="quiet small">
                {base.source === 'cloud' ? m.bs_link_hint_cloud() : m.bs_link_hint_simulated()}
              </p>
            </div>
          {/if}
        </section>

        <section class="block" aria-label={m.bs_fw()}>
          <h3>{m.bs_fw()}</h3>
          <BaseStationFirmware
            {info}
            reported={found?.state.firmware ?? null}
            relays={relaysOf(base, info)}
            blocker={accountBlocker}
            checking={checking === base.id}
            request={requests[base.id] ?? null}
            now={clock.now}
            oncheck={() => void check(base)}
            oninstall={(version, mowerId) => void install(base, version, mowerId)}
          />
        </section>

        {#if confirmDelete === base.id}
          <div class="danger" data-testid="bs-delete-confirm">
            <p>{m.bs_delete_confirm({ name: base.name })}</p>
            <div class="row">
              <Button
                variant="danger"
                icon="trash"
                pending={busy === `delete:${base.id}`}
                testid="bs-delete-go"
                onclick={() => void remove(base)}>{m.bs_delete()}</Button
              >
              <Button variant="ghost" onclick={() => (confirmDelete = null)}>{m.cancel()}</Button>
            </div>
          </div>
        {/if}
      </li>
    {/each}
  </ul>
{/if}

<Sheet
  open={registerOpen}
  title={m.bs_register_title()}
  closeLabel={m.close()}
  onclose={() => (registerOpen = false)}
  testid="bs-register"
>
  <div class="register">
    <p class="quiet">{m.bs_register_intro()}</p>
    {#if candidates === null}
      <p class="quiet" role="status"><SlowLoading /></p>
    {:else}
      {#each candidates as candidate (candidate.account.id)}
        {#if candidate.devices.length}
          <section class="account">
            <h3>{candidate.account.email}</h3>
            <ul class="devices">
              {#each candidate.devices as device (device.device_name)}
                {@const taken = device.registered || device.registered_elsewhere}
                <li class="device" data-testid="bs-device" data-device={device.device_name}>
                  <span class="device-name">
                    <span>{device.device_name}</span>
                    <span class="meta mono"
                      >{[
                        device.model,
                        device.online === false ? m.bs_register_offline() : null,
                        device.registered
                          ? m.bs_register_registered()
                          : device.registered_elsewhere
                            ? m.bs_register_elsewhere()
                            : null,
                      ]
                        .filter(Boolean)
                        .join(' · ')}</span
                    >
                  </span>
                  <Button
                    icon="plus"
                    disabled={taken || busy === 'register'}
                    testid="bs-device-add"
                    onclick={() =>
                      void register(
                        {
                          source: 'cloud',
                          linked_account_id: candidate.account.id,
                          device_name: device.device_name,
                        },
                        device.device_name,
                      )}>{m.bs_register_go()}</Button
                  >
                </li>
              {/each}
            </ul>
          </section>
        {/if}
      {/each}
      {#if deviceCount === 0}
        <p class="quiet" data-testid="bs-register-none">{m.bs_register_none()}</p>
        <Button variant="ghost" href="/settings#linked-accounts" icon="link"
          >{m.bs_register_accounts_link()}</Button
        >
      {/if}
      {#if simulatedAllowed}
        <section class="account">
          <ul class="devices">
            <li class="device" data-testid="bs-device-simulated">
              <span class="device-name">
                <span>{m.bs_register_simulated()}</span>
                <span class="meta">{m.bs_register_simulated_hint()}</span>
              </span>
              <Button
                icon="plus"
                disabled={busy === 'register'}
                testid="bs-simulated-add"
                onclick={() =>
                  void register(
                    { source: 'simulated', name: m.bs_register_simulated() },
                    m.bs_register_simulated(),
                  )}>{m.bs_register_go()}</Button
              >
            </li>
          </ul>
        </section>
      {/if}
    {/if}
    {#if registerProblem}
      <ProblemNotice title={registerProblem} />
    {/if}
  </div>
</Sheet>

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
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .empty {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--md-space-3);
    max-width: 560px;
    padding: var(--md-space-8) var(--md-gutter);
  }

  .empty h2 {
    font-size: var(--md-font-size-24);
    line-height: var(--md-line-32);
  }

  .mark {
    display: grid;
    place-items: center;
    width: 56px;
    height: 56px;
    border: var(--md-border-control) solid var(--md-color-ink);
  }

  .bases {
    display: grid;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .base {
    display: grid;
    gap: var(--md-space-4);
    padding: var(--md-space-4) var(--md-gutter) var(--md-space-6);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  .head {
    display: flex;
    align-items: flex-start;
    gap: var(--md-space-3);
  }

  .glyph {
    display: grid;
    flex: none;
    place-items: center;
    width: 40px;
    height: 40px;
    border: var(--md-hairline) solid var(--md-color-line-strong);
  }

  .titles {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: var(--md-space-0_5);
    min-width: 0;
  }

  /* A device name reads as a name, not as a section label (the global h2). */
  h2 {
    margin: 0;
    color: var(--md-color-ink);
    font-size: var(--md-font-size-20);
    line-height: var(--md-line-28);
    font-weight: var(--md-weight-semibold);
    letter-spacing: normal;
    text-transform: none;
    overflow-wrap: anywhere;
  }

  h3 {
    margin: 0 0 var(--md-space-2);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .meta {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .tools {
    display: flex;
    gap: var(--md-space-1);
  }

  .rename {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--md-space-2);
  }

  .block {
    display: grid;
    align-content: start;
    gap: var(--md-space-2);
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    min-height: 40px;
    padding: 0 var(--md-space-1) 0 var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    font-size: var(--md-font-size-14);
  }

  .chip a {
    color: var(--md-color-ink);
    text-decoration: none;
  }

  .chip-x {
    display: grid;
    place-items: center;
    width: 32px;
    height: 32px;
    border: 0;
    background: none;
    color: var(--md-color-ink-muted);
    cursor: pointer;
  }

  .chip-x:hover {
    color: var(--md-state-error);
  }

  .chip a:focus-visible,
  .chip-x:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .assign {
    display: grid;
    gap: var(--md-space-2);
    max-width: 480px;
  }

  .danger {
    display: grid;
    gap: var(--md-space-2);
    padding: var(--md-space-3);
    border-left: 3px solid var(--md-state-error);
    background: var(--md-state-error-bg);
  }

  .danger p {
    margin: 0;
  }

  .row {
    display: flex;
    gap: var(--md-space-2);
  }

  .register {
    display: grid;
    gap: var(--md-space-4);
  }

  .account h3 {
    margin-bottom: var(--md-space-1);
    overflow-wrap: anywhere;
  }

  .devices {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .device {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-3);
    padding: var(--md-space-2) 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .device-name {
    display: grid;
    gap: var(--md-space-0_5);
    min-width: 0;
    overflow-wrap: anywhere;
  }

  @media (min-width: 1024px) {
    .base {
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      column-gap: var(--md-space-8);
    }

    .head,
    .danger {
      grid-column: 1 / -1;
    }

    .block:first-of-type {
      grid-row: span 2;
    }
  }
</style>

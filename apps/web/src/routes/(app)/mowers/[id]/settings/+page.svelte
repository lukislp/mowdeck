<script lang="ts">
  import { Button, Field, Panel, ProblemNotice, Select, TopBar } from '@mowdeck/ui';
  import { tick } from 'svelte';

  import { goto } from '$app/navigation';
  import { page } from '$app/state';

  import { fleetApi, telemetryApi, unwrap } from '$lib/api/client';
  import { asProblem, type ApiProblem } from '$lib/api/problem';
  import { clock } from '$lib/clock.svelte';
  import BridgeLinkPanel from '$lib/components/BridgeLinkPanel.svelte';
  import BridgeLinkSheet from '$lib/components/BridgeLinkSheet.svelte';
  import ConnectionForm from '$lib/components/ConnectionForm.svelte';
  import DriveCalibrationPanel from '$lib/components/DriveCalibrationPanel.svelte';
  import DriveSpeedControl from '$lib/components/DriveSpeedControl.svelte';
  import FirmwarePanel from '$lib/components/FirmwarePanel.svelte';
  import MowerPlacement from '$lib/components/MowerPlacement.svelte';
  import SiteLocation from '$lib/components/SiteLocation.svelte';
  import SlowLoading from '$lib/components/SlowLoading.svelte';
  import {
    buildOptions,
    initialState,
    serverIssues,
    validate,
    type ConnectionState,
    type FieldIssue,
    type KindSchema,
  } from '$lib/connection/form';
  import { loadKinds } from '$lib/connection/kinds';
  import { kindTitle } from '$lib/connection/text';
  import { deleteDriveCalibration, putDriveCalibration } from '$lib/fleet/drive-calibration';
  import {
    checkFirmware,
    firmwareCheckProblemText,
    shownFirmwareInfo,
    type FirmwareInfo,
  } from '$lib/fleet/firmware';
  import {
    BLE_BRIDGE,
    bridgeLinkOfMower,
    checkBridge,
    deleteBridgeLink,
    putBridgeLink,
    scanBridge,
    type BridgeLink,
  } from '$lib/fleet/links';
  import type { components } from '$lib/generated/fleet';
  import { connectionLabel, problemMessage } from '$lib/i18n';
  import { isLegacyCloud, type LinkedAccount } from '$lib/linked/accounts';
  import { getLinkedAccount } from '$lib/linked/api';
  import { commands, permissions, realtime, sendCommand } from '$lib/live.svelte';
  import { BridgeLinkFlow } from '$lib/mower/bridge-link.svelte';
  import {
    calibrationConditions,
    driveSpeedLocked,
    storedCalibrationOf,
    type DriveSpeedSetting,
  } from '$lib/mower/drive-calibration';
  import { DriveCalibrationFlow } from '$lib/mower/drive-calibration.svelte';
  import { m } from '$lib/paraglide/messages';
  import { mowers as states } from '$lib/stores/mowers.svelte';
  import { toasts } from '$lib/stores/ui.svelte';

  type Mower = components['schemas']['MowerOut'];
  type ConnectionKind = components['schemas']['ConnectionKind'];
  /** What this page reads of the capabilities (structural, like the other pages). */
  type Capabilities = {
    links_supported?: string[];
    firmware_update?: boolean;
    manual_drive?: boolean;
    drive_speed_setting?: DriveSpeedSetting | null;
  };

  const EMPTY: ConnectionState = { values: {}, flags: {}, secretSet: {}, choice: {} };

  const id = $derived(page.params.id ?? '');
  let mower = $state<Mower | null>(null);
  // The bridge link and the firmware panel read the capabilities and the live state (ADR 0038).
  let caps = $state<Capabilities | null>(null);
  let bridgeLink = $state<BridgeLink | null>(null);
  let canControl = $state(false);
  let canDrive = $state(false);
  let checkingFirmware = $state(false);
  /** The answer of the last "Nach Updates suchen", until the telemetry reports a newer check. */
  let checkedFirmware = $state<FirmwareInfo | null>(null);
  let kinds = $state<KindSchema[]>([]);
  let name = $state('');
  let kind = $state('simulated');
  let model = $state('');
  let serial = $state('');
  let connection = $state<ConnectionState>(EMPTY);
  // Client checks run live after the first submit; Fleet's 422 field errors stay until the next.
  let tried = $state(false);
  let serverFound = $state<Record<string, FieldIssue>>({});
  let busy = $state(false);
  let confirmDelete = $state(false);
  let problem = $state<ApiProblem | null>(null);
  let origin = $state<{ origin_lat: number; origin_lon: number } | null>(null);
  let linkedAccount = $state<LinkedAccount | null>(null);
  let linkedUnreadable = $state(false);

  const schema = $derived(kinds.find((k) => k.kind === kind) ?? null);
  const issues = $derived(
    tried && schema ? { ...serverFound, ...validate(schema, connection) } : serverFound,
  );
  const sameKind = $derived(mower !== null && kind === mower.connection_kind);
  const stored = $derived(sameKind ? (mower?.connection_options ?? null) : null);
  // A cloud mower from before linked accounts: its login lives in its options; shown read-only.
  const legacy = $derived(mower !== null && sameKind && isLegacyCloud(mower));
  // Mowers of a linked account kind are added from the account, not converted here.
  const toLinked = $derived(!sameKind && schema?.linked_account === true);
  const storedEntries = $derived(Object.entries(mower?.connection_options ?? {}));
  const extras = $derived.by(() => {
    if (!stored || !schema) return {};
    const known = new Set(schema.options.map((o) => o.key));
    return Object.fromEntries(Object.entries(stored).filter(([key]) => !known.has(key)));
  });
  const kindOptions = $derived.by(() => {
    const list = kinds.map((k) => ({ value: k.kind, label: kindTitle(k) }));
    if (mower && !kinds.some((k) => k.kind === mower?.connection_kind)) {
      list.push({ value: mower.connection_kind, label: connectionLabel(mower.connection_kind) });
    }
    return list;
  });

  // --- Bluetooth bridge and firmware (ADR 0038) -----------------------------------------------

  const snap = $derived(states.get(id));
  const ms = $derived(snap?.state ?? null);
  const stale = $derived(states.isOffline(id, clock.now));
  const bridgeStatus = $derived(ms?.links?.[BLE_BRIDGE] ?? null);
  /** The panel shows when the connection can open a bridge, or while a link is stored. */
  const bridgeOffered = $derived(
    bridgeLink !== null || (caps?.links_supported ?? []).includes(BLE_BRIDGE),
  );
  /** The newer of the telemetry's firmware info and the last check's answer. */
  const firmwareInfo = $derived(shownFirmwareInfo(ms?.firmware_info ?? null, checkedFirmware));

  const bridge = new BridgeLinkFlow({
    scan: (input, signal) => scanBridge(id, input, signal),
    save: async (input) => {
      const saved = await putBridgeLink(id, input);
      bridgeLink = saved;
      toasts.push('ok', m.bridge_saved());
      return saved;
    },
    expectedName: () => {
      const value = mower?.connection_options?.device_name;
      return typeof value === 'string' && value !== '' ? value : null;
    },
    existing: () => bridgeLink,
  });

  // --- manual driving: speed setting and drive calibration (ADR 0042) -------------------------

  const speedSetting = $derived(caps?.drive_speed_setting ?? null);
  const calibration = new DriveCalibrationFlow({
    command: (command) => sendCommand(id, command),
    save: async (input) => {
      const saved = await putDriveCalibration(id, input);
      toasts.push('ok', m.calib_saved());
      return saved;
    },
    remove: async () => {
      await deleteDriveCalibration(id);
      toasts.push('ok', m.calib_removed());
    },
    status: () => ms?.drive_calibration ?? null,
    stored: () => storedCalibrationOf(mower),
  });
  const calibConditions = $derived(
    calibrationConditions({ state: ms, manualDrive: caps?.manual_drive, now: clock.now }),
  );
  /** The section shows when the mower drives manually, has a speed setting or a stored fit. */
  const driveOffered = $derived(
    caps?.manual_drive === true || speedSetting !== null || calibration.stored !== null,
  );

  async function runFirmwareCheck(): Promise<void> {
    if (checkingFirmware) return;
    checkingFirmware = true;
    try {
      const info = await checkFirmware(id);
      checkedFirmware = info;
      toasts.push(
        'ok',
        info.available && info.update_state !== 'idle'
          ? m.firmware_check_available({ version: info.available })
          : m.firmware_check_none(),
      );
    } catch (error) {
      const p = asProblem(error);
      toasts.push(
        'error',
        firmwareCheckProblemText(p),
        p.detail ?? undefined,
        p.correlationId ?? undefined,
      );
    } finally {
      checkingFirmware = false;
    }
  }

  function installFirmware(version: string): void {
    void sendCommand(id, { kind: 'firmware_update', version });
  }

  function show(found: Mower): void {
    if (found.linked_account_id && found.linked_account_id !== mower?.linked_account_id) {
      loadLinkedAccount(found.linked_account_id);
    }
    mower = found;
    bridgeLink = bridgeLinkOfMower(found);
    calibration.syncStored(storedCalibrationOf(found));
    name = found.name;
    model = found.model ?? '';
    serial = found.serial ?? '';
    selectKind(found.connection_kind);
  }

  function selectKind(next: string): void {
    kind = next;
    const found = kinds.find((k) => k.kind === next);
    const options = mower && next === mower.connection_kind ? mower.connection_options : null;
    connection = found ? initialState(found, options) : EMPTY;
    tried = false;
    serverFound = {};
  }

  function loadLinkedAccount(accountId: string): void {
    linkedUnreadable = false;
    void getLinkedAccount(accountId)
      .then((found) => (linkedAccount = found))
      .catch(() => (linkedUnreadable = true));
  }

  $effect(() => {
    void (async () => {
      try {
        const [found, available] = await Promise.all([
          unwrap(
            fleetApi.GET('/api/v1/fleet/mowers/{mower_id}', { params: { path: { mower_id: id } } }),
          ),
          loadKinds(),
        ]);
        kinds = available;
        show(found);
        const path = { params: { path: { mower_id: id } } };
        // The map's geo origin is offered as the site's coordinates (rain pause location).
        void unwrap(telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/map', path))
          .then((snapshot) => (origin = snapshot.map.reference))
          .catch(() => undefined);
        // Capabilities decide the bridge panel; the state feeds the link status and firmware.
        void unwrap(telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/capabilities', path))
          .then((snapshot) => (caps = snapshot.capabilities))
          .catch(() => (caps = null));
        void unwrap(telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/state', path))
          .then((snapshot) => states.upsert(id, snapshot.state, 'rest'))
          .catch(() => undefined);
        void permissions
          .check(id, ['can_control', 'can_drive'])
          .then((result) => {
            canControl = result.can_control;
            canDrive = result.can_drive;
          })
          .catch(() => {
            canControl = false;
            canDrive = false;
          });
      } catch (error) {
        problem = asProblem(error);
      }
    })();
  });
  $effect(() => realtime.watch([id]));
  $effect(() => clock.hold(5_000));
  $effect(() => {
    void id;
    caps = null;
    checkedFirmware = null;
  });

  async function focusFirstIssue(): Promise<void> {
    await tick();
    document.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
  }

  async function save(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!mower) return;
    problem = null;
    tried = true;
    serverFound = {};
    if (schema && Object.keys(validate(schema, connection)).length > 0) {
      await focusFirstIssue();
      return;
    }
    busy = true;
    try {
      const saved = await unwrap(
        fleetApi.PATCH('/api/v1/fleet/mowers/{mower_id}', {
          params: { path: { mower_id: id } },
          body: {
            name: name.trim(),
            model: model.trim() || null,
            serial: serial.trim() || null,
            ...(sameKind ? {} : { connection_kind: kind as ConnectionKind }),
            // Untouched secrets go out as *** (Fleet keeps the stored value); managed keys and
            // the options of a legacy cloud mower are never sent.
            ...(schema && !legacy && !toLinked
              ? { connection_options: buildOptions(schema, connection, stored) }
              : {}),
          },
        }),
      );
      show(saved);
      toasts.push('ok', m.saved());
    } catch (error) {
      problem = asProblem(error);
      if (schema) serverFound = serverIssues(schema, problem);
      await focusFirstIssue();
    } finally {
      busy = false;
    }
  }

  async function remove(): Promise<void> {
    if (!confirmDelete) {
      confirmDelete = true;
      return;
    }
    busy = true;
    try {
      await unwrap(
        fleetApi.DELETE('/api/v1/fleet/mowers/{mower_id}', { params: { path: { mower_id: id } } }),
      );
      toasts.push('ok', m.mower_deleted());
      await goto('/mowers');
    } catch (error) {
      problem = asProblem(error);
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>{m.mower_settings_title()} · {m.app_name()}</title></svelte:head>

<TopBar
  title={m.mower_settings_title()}
  meta={mower?.name}
  backHref="/mowers/{id}"
  backLabel={m.back()}
/>

{#if problem && !mower}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if mower}
  <form class="form" onsubmit={save} novalidate>
    <Panel label={m.mower_settings_general()}>
      <div class="fields">
        <Field id="name" label={m.mower_name()} required maxlength={100} bind:value={name} />
        <Select
          id="connection_kind"
          label={m.mower_connection_kind()}
          options={kindOptions}
          bind:value={() => kind, selectKind}
          hint={problem?.fieldError('connection_kind') ??
            (sameKind ? undefined : m.mower_kind_change_warning())}
        />
        <Field id="model" label={m.mower_model()} hint={m.optional()} bind:value={model} />
        <Field id="serial" label={m.mower_serial()} hint={m.optional()} mono bind:value={serial} />
      </div>
    </Panel>
    <Panel label={m.mower_connection_title()}>
      {#if sameKind && mower.linked_account_id}
        <div class="via" data-testid="linked-via">
          <span class="label">{m.linked_via()}</span>
          <a href="/settings#linked-accounts" data-testid="linked-via-email"
            >{linkedAccount?.email ?? (linkedUnreadable ? m.linked_account_unreadable() : '…')}</a
          >
        </div>
      {/if}
      {#if legacy}
        <div class="legacy" data-testid="linked-legacy">
          <p class="hint">{m.linked_legacy_hint()}</p>
          <ul>
            {#each storedEntries as [key, value] (key)}
              <li><span class="key">{key}</span><span>{JSON.stringify(value)}</span></li>
            {/each}
          </ul>
        </div>
      {:else if toLinked}
        <p class="hint" data-testid="linked-kind-change">{m.linked_kind_change_hint()}</p>
      {:else if schema}
        <ConnectionForm {schema} bind:state={connection} {issues} {extras} />
      {:else}
        <p class="hint">{m.conn_unknown_kind()}</p>
      {/if}
    </Panel>
    <div class="actions">
      {#if problem}
        <ProblemNotice
          title={problemMessage(problem)}
          detail={problem.detail}
          reference={problem.correlationId}
          referenceLabel={m.problem_reference()}
        />
      {/if}
      <Button
        type="submit"
        variant="primary"
        pending={busy}
        disabled={!name.trim() || toLinked}
        testid="mower-save">{m.save()}</Button
      >
    </div>
  </form>
  <div class="links">
    {#if bridgeOffered}
      <Panel label={m.bridge_title()} testid="bridge-panel">
        <BridgeLinkPanel
          link={bridgeLink}
          status={bridgeStatus}
          onadd={() => bridge.start()}
          check={() => checkBridge(id)}
          remove={() => deleteBridgeLink(id)}
          onremoved={() => (bridgeLink = null)}
        />
      </Panel>
    {/if}
    {#if driveOffered}
      <Panel label={m.drive_settings_title()} testid="drive-section">
        <div class="drive">
          {#if speedSetting}
            <DriveSpeedControl
              setting={speedSetting}
              current={ms?.drive_speed_mps ?? null}
              locked={driveSpeedLocked(ms)}
              pending={commands.pending(id, 'set_drive_speed')}
              allowed={canDrive}
              onset={(command) => void sendCommand(id, command)}
            />
          {/if}
          <DriveCalibrationPanel
            flow={calibration}
            conditions={calibConditions}
            allowed={canDrive}
            currentSpeed={ms?.drive_speed_mps ?? null}
            now={clock.now}
          />
        </div>
      </Panel>
    {/if}
    <Panel label={m.firmware_title()} testid="firmware-section">
      <FirmwarePanel
        info={firmwareInfo}
        reported={ms?.firmware ?? null}
        capable={caps?.firmware_update === true}
        activity={ms?.activity ?? null}
        batteryPercent={ms?.battery_percent ?? null}
        stale={snap === undefined || stale}
        allowed={canControl}
        checking={checkingFirmware}
        now={clock.now}
        oncheck={() => void runFirmwareCheck()}
        oninstall={installFirmware}
      />
    </Panel>
  </div>
  <BridgeLinkSheet flow={bridge} />
  <div class="site">
    <MowerPlacement
      {mower}
      onchange={(saved) => {
        if (mower) mower = { ...mower, site_id: saved.site_id, group_ids: saved.group_ids };
      }}
    />
    <SiteLocation siteId={mower.site_id} {origin} />
  </div>
  <Panel label={m.mower_delete_title()}>
    <p class="hint">{m.mower_delete_body()}</p>
    <div class="danger">
      <Button variant="danger" icon="trash" pending={busy} onclick={remove} testid="mower-delete"
        >{confirmDelete ? m.mower_delete() : m.mower_delete_title()}</Button
      >
      {#if confirmDelete}
        <Button variant="ghost" onclick={() => (confirmDelete = false)}>{m.cancel()}</Button>
      {/if}
    </div>
  </Panel>
{:else}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .quiet {
    color: var(--md-color-ink-muted);
  }

  .form {
    max-width: 720px;
  }

  .fields {
    display: grid;
    gap: var(--md-space-4);
  }

  .hint {
    margin-bottom: var(--md-space-3);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .actions {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--md-space-3);
    padding: 0 var(--md-gutter) var(--md-space-6);
  }

  .site,
  .links {
    max-width: 720px;
  }

  .via {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--md-space-2);
    margin-bottom: var(--md-space-4);
    padding: var(--md-space-2) var(--md-space-3);
    border-left: 3px solid var(--md-color-ink);
    background: var(--md-color-surface-sunken);
  }

  .label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .via a {
    color: var(--md-color-ink);
    font-weight: var(--md-weight-semibold);
    overflow-wrap: anywhere;
  }

  .legacy ul {
    margin: 0;
    padding: 0;
    list-style: none;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
  }

  .legacy li {
    display: flex;
    gap: var(--md-space-2);
    overflow-wrap: anywhere;
  }

  .key {
    color: var(--md-color-ink-muted);
  }

  .drive {
    display: grid;
    gap: var(--md-space-6);
  }

  .danger {
    display: flex;
    gap: var(--md-space-2);
  }

  @media (min-width: 768px) {
    .fields {
      grid-template-columns: 1fr 1fr;
    }
  }
</style>

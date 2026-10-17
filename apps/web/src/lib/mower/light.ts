/**
 * The mower's switchable light (the camera's headlamp), one implementation for every place that
 * offers the switch: the instrument tile on the mower page (`LightToggle`) and the glass chip over
 * the drive view's camera (`LightChip`). Pure, so it is unit tested without a page.
 *
 * Decided from the capabilities only (`capabilities.light`), never from the vendor. The switch
 * shows the state the mower reports, never an optimistic one: while a `set_light` command is on
 * its way it waits, and a tap always asks for the opposite of what the mower reported.
 */

/** What the switch knows about the mower and the viewer. */
export interface LightInput {
  /** What the mower reports: true on, false off, null not reported. */
  on: boolean | null;
  /** The mower has a switchable light (`capabilities.light`). */
  capable: boolean;
  online: boolean;
  /** The viewer may control the mower (`can_control`). */
  allowed: boolean;
  /** A `set_light` command is on its way. */
  pending: boolean;
}

export type LightState = 'on' | 'off' | 'unknown';

/** Why the switch cannot be used: a key of the `light_*` reason messages. */
export type LightReason = 'unsupported' | 'offline' | 'no_permission';

export interface LightView {
  state: LightState;
  reason: LightReason | null;
  /** The switch takes no tap (a reason, or a command on its way). */
  disabled: boolean;
  /** What a tap asks for. */
  next: boolean;
}

/** The capability flag, from the capabilities alone. */
export function lightCapable(caps: { light?: boolean | null } | null | undefined): boolean {
  return caps?.light === true;
}

export function lightView(input: LightInput): LightView {
  const reason: LightReason | null = !input.capable
    ? 'unsupported'
    : !input.online
      ? 'offline'
      : !input.allowed
        ? 'no_permission'
        : null;
  return {
    state: input.on === true ? 'on' : input.on === false ? 'off' : 'unknown',
    reason,
    disabled: reason !== null || input.pending,
    next: input.on !== true,
  };
}

/** The command a tap sends. */
export function setLight(on: boolean): { kind: 'set_light'; on: boolean } {
  return { kind: 'set_light', on };
}

/**
 * A switched light needs no "executed" toast while the mower is driven by hand: the chip over the
 * camera shows the reported state, and a toast would land on the overlay stick. Failures still
 * toast, and outside a drive session (the tile on the mower page) every outcome does.
 */
export function quietLightResult(
  command: { kind: string; status: string; mowerId: string },
  drivenMowerId: string | null,
): boolean {
  return (
    command.kind === 'set_light' &&
    command.status === 'executed' &&
    drivenMowerId !== null &&
    command.mowerId === drivenMowerId
  );
}

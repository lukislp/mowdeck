/**
 * Turns joystick samples into drive frames (ADR 0026, the drive page).
 *
 * The pad samples at the server's `max_rate_hz` while it is held. A deflected stick sends every
 * sample (the Realtime client drops anything above the rate); a centred stick that is still held
 * sends a zero frame at 2 Hz only, to keep the session alive without traffic; letting go sends one
 * zero frame (forced past the rate limit) and then nothing, so the Device-Gateway's dead-man timer
 * is the backstop. The speed limit scales `linear` only; blades ride along only when the page has
 * armed and switched them on, and never in the release frame. Backwards frames go out only when
 * the drive link can reverse (`reverseAllowed`, `clampReverse`). The pilot also counts frames sent
 * and acknowledged in the last second for the session strip.
 */
import { isZero, limitVector, type StickVector } from '@mowdeck/ui';

import type { DriveFrame } from '$lib/realtime/client.svelte';

/** A held, centred stick refreshes the session this often. */
export const IDLE_FRAME_MS = 500;
export const SPEED_LIMITS = [25, 50, 100] as const;
export type SpeedLimit = (typeof SPEED_LIMITS)[number];

/**
 * The stick as the drive link can carry it: without reverse driving
 * (`capabilities.drive_reverse` false, ADR 0043) the backwards part is cut to zero, so no frame
 * with a negative `linear` ever goes out; turning keeps its range.
 */
export function clampReverse(vector: StickVector, reverseAllowed: boolean): StickVector {
  if (reverseAllowed || vector.linear >= 0) return vector;
  return { linear: 0, angular: vector.angular };
}

export interface DriveLink {
  sendFrame(frame: DriveFrame, force?: boolean): boolean;
}

export class DrivePilot {
  /** Percent of full forward speed. */
  speedLimit: SpeedLimit = 25;
  /** Blades on in the frames (the page decides: capability, armed, switched). */
  bladeOn = false;
  /**
   * The drive link can drive backwards (`capabilities.drive_reverse`); off by default, so an
   * unknown link never gets a backwards frame.
   */
  reverseAllowed = false;

  readonly #link: DriveLink;
  readonly #now: () => number;
  #lastSentAt = -Infinity;
  #sent: number[] = [];
  #acked: number[] = [];

  constructor(link: DriveLink, now: () => number = Date.now) {
    this.#link = link;
    this.#now = now;
  }

  /** One sample of the held stick; returns whether a frame went out. */
  sample(vector: StickVector): boolean {
    const limited = limitVector(clampReverse(vector, this.reverseAllowed), this.speedLimit / 100);
    const now = this.#now();
    if (isZero(limited) && now - this.#lastSentAt < IDLE_FRAME_MS) return false;
    return this.#send(limited, false);
  }

  /** The stick was let go: one zero frame, then silence. */
  release(): boolean {
    return this.#send({ linear: 0, angular: 0 }, true);
  }

  /** The server acknowledged a frame. */
  acked(): void {
    this.#acked.push(this.#now());
  }

  /** Frames sent and acknowledged within the last second. */
  rates(): { sent: number; acked: number } {
    const since = this.#now() - 1000;
    this.#sent = this.#sent.filter((t) => t > since);
    this.#acked = this.#acked.filter((t) => t > since);
    return { sent: this.#sent.length, acked: this.#acked.length };
  }

  /** Forget the counters (a new session). */
  reset(): void {
    this.#lastSentAt = -Infinity;
    this.#sent = [];
    this.#acked = [];
    this.bladeOn = false;
  }

  #send(vector: StickVector, force: boolean): boolean {
    const frame: DriveFrame = { ...vector, blade_on: this.bladeOn && !force };
    if (!this.#link.sendFrame(frame, force)) return false;
    const now = this.#now();
    this.#lastSentAt = now;
    this.#sent.push(now);
    return true;
  }
}

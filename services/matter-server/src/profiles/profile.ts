/**
 * A Matter device profile: how a mower appears on the Matter side. Matter has no mower device
 * type up to 1.5, so the one profile today is the Robotic Vacuum Cleaner (`rvc.ts`); when the CSA
 * publishes a mower type, a second profile implements this interface and the node manager picks
 * it by name (`MOWDECK_MATTER_DEVICE_PROFILE`). Nothing outside `profiles/` knows a cluster.
 */
import type { Endpoint } from '@matter/main';

import type { Intent, MowerSnapshot, RefuseReason } from '../model.ts';

/** Labels a controller may show (run modes, power source), in the configured language. */
export interface ProfileLabels {
  idle: string;
  mowing: string;
  battery: string;
}

export const LABELS: Record<'de' | 'en', ProfileLabels> = {
  de: { idle: 'Bereit', mowing: 'Mähen', battery: 'Akku' },
  en: { idle: 'Ready', mowing: 'Mowing', battery: 'Battery' },
};

/** The answer to a controller's command: done, or refused with a reason. */
export type IntentResult = { ok: true } | { ok: false; reason: RefuseReason | 'failed' };

/** Called by the profile's behaviors when a controller sends a command. */
export type IntentHandler = (intent: Intent) => Promise<IntentResult>;

export interface DeviceProfile {
  /** Profile name (`MOWDECK_MATTER_DEVICE_PROFILE`). */
  readonly name: string;
  /** Matter device type id announced while commissioning. */
  readonly deviceTypeId: number;
  /** The mower's endpoint in the initial state `snapshot`; `onIntent` receives its commands. */
  createEndpoint(snapshot: MowerSnapshot, labels: ProfileLabels, onIntent: IntentHandler): Endpoint;
  /** Show `snapshot` on an endpoint this profile created. */
  apply(endpoint: Endpoint, snapshot: MowerSnapshot): Promise<void>;
}

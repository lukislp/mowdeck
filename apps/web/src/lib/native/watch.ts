/**
 * The Apple Watch app of the iOS shell (ADR 0060), as far as the settings page needs it.
 *
 * The watch holds no credential and never calls the server: it relays every command through
 * the iPhone app (WatchConnectivity), which runs it with the device credential of the widgets
 * (ADR 0059). So the watch works exactly when a watch with the app is paired and widgets are on
 * here. This module only reads that state from the shell (`MowDeckShell.getWatchStatus`) and
 * asks the shell to send the current state to the watch (`syncWatch`).
 */
import type { NativePlugin } from './capacitor';
import type { WidgetsState } from './widgets';

export type WatchState =
  /** No WatchConnectivity (iPad, Android, a browser, an older iOS build). */
  | 'unsupported'
  /** No Apple Watch paired with this iPhone. */
  | 'unpaired'
  /** A watch is paired, but the MowDeck watch app is not installed on it. */
  | 'not-installed'
  /** Watch app installed, but widgets are off here, so the watch has nothing to relay through. */
  | 'needs-widgets'
  | 'ready';

export interface WatchStatus {
  supported?: boolean;
  paired?: boolean;
  installed?: boolean;
  reachable?: boolean;
  complication?: boolean;
}

/** The state shown on the settings page. */
export function watchStateOf(status: WatchStatus | null, widgets: WidgetsState): WatchState {
  if (!status?.supported) return 'unsupported';
  if (!status.paired) return 'unpaired';
  if (!status.installed) return 'not-installed';
  return widgets === 'on' ? 'ready' : 'needs-widgets';
}

/** Read the watch's status from the shell; shells without the method count as unsupported. */
export async function readWatchStatus(shell: NativePlugin | null): Promise<WatchStatus | null> {
  if (!shell) return null;
  try {
    return (await shell.call<WatchStatus>('getWatchStatus')) ?? null;
  } catch {
    return null;
  }
}

/** Ask the shell to send the current state to the watch (after widgets were set up). */
export async function syncWatch(shell: NativePlugin | null): Promise<void> {
  await shell?.call('syncWatch').catch(() => undefined);
}

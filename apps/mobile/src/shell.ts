/**
 * `MowDeckShell`: the shell's own native plugin (ios/App/App/MowDeckShellPlugin.swift,
 * android/app/src/main/java/app/mowdeck/mobile/MowDeckShellPlugin.java). It stores the server
 * address natively, where the shell reads it at start to load the web app from that origin.
 * On iOS it also keeps the installation id for the APNs registration in the Keychain (the web
 * app calls `getInstallationId` through the injected bridge, `apps/web/src/lib/native`).
 */
import { registerPlugin } from '@capacitor/core';

export interface MowDeckShellPlugin {
  /** The stored server origin, null before the first launch screen was completed. */
  getServerUrl(): Promise<{ url: string | null }>;
  /** Store the origin and reload the shell with the web app of that server. */
  setServerUrl(options: { url: string }): Promise<void>;
  /** Forget the server and show the first-launch screen again. */
  resetServerUrl(): Promise<void>;
  /** Load the stored server again (after it was unreachable). */
  reload(): Promise<void>;
  /**
   * iOS only: this installation's id (a UUID created once, kept in the Keychain with
   * `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`: survives updates and reinstalls on this
   * device, not synced, not restored onto another device). Android has no APNs and no method.
   */
  getInstallationId(): Promise<{ id: string }>;
}

export const MowDeckShell = registerPlugin<MowDeckShellPlugin>('MowDeckShell');

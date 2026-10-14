/**
 * `MowDeckShell`: the shell's own native plugin (ios/App/App/MowDeckShellPlugin.swift,
 * android/app/src/main/java/app/mowdeck/mobile/MowDeckShellPlugin.java). It stores the server
 * address natively, where the shell reads it at start to load the web app from that origin.
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
}

export const MowDeckShell = registerPlugin<MowDeckShellPlugin>('MowDeckShell');

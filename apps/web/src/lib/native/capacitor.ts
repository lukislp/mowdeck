/**
 * The Capacitor bridge of the native app shells (apps/mobile, ADR 0036), as the web app sees it.
 *
 * The shells load this web app from the MowDeck server and inject `window.Capacitor` (the
 * native bridge script of `@capacitor/ios` and `@capacitor/android`) into it. The web app does
 * not bundle `@capacitor/core`: plugins are called through the injected bridge's
 * `nativePromise` and `addListener`, and `PluginHeaders` lists the plugins the shell has. In a
 * browser there is no bridge and everything here is inert.
 */

export type NativePlatform = 'ios' | 'android';

interface PluginHeader {
  name: string;
}

/** The part of the injected `window.Capacitor` the web app uses. */
export interface CapacitorBridge {
  isNativePlatform?: () => boolean;
  getPlatform?: () => string;
  nativePromise?: (plugin: string, method: string, options?: object) => Promise<unknown>;
  addListener?: (
    plugin: string,
    event: string,
    callback: (data: unknown) => void,
  ) => { remove: () => unknown };
  PluginHeaders?: PluginHeader[];
}

/** A native plugin of the shell. */
export interface NativePlugin {
  call<T = unknown>(method: string, options?: object): Promise<T>;
  /** Subscribe to a plugin event; returns the unsubscribe function. */
  on(event: string, callback: (data: unknown) => void): () => void;
}

/** The shell's bridge with its platform, or null in a browser. */
export interface NativeBridge {
  platform: NativePlatform;
  has(plugin: string): boolean;
  plugin(name: string): NativePlugin;
}

/** The bridge of `scope` (the window), when the page runs inside a MowDeck app shell. */
export function nativeBridge(scope: unknown = globalThis): NativeBridge | null {
  const cap = (scope as { Capacitor?: CapacitorBridge } | undefined)?.Capacitor;
  if (!cap || typeof cap.isNativePlatform !== 'function' || !cap.isNativePlatform()) return null;
  const platform = cap.getPlatform?.();
  if (platform !== 'ios' && platform !== 'android') return null;
  const { nativePromise, addListener } = cap;
  if (typeof nativePromise !== 'function' || typeof addListener !== 'function') return null;
  return {
    platform,
    has: (name) => (cap.PluginHeaders ?? []).some((header) => header.name === name),
    plugin: (name) => ({
      call: <T>(method: string, options?: object) =>
        nativePromise(name, method, options ?? {}) as Promise<T>,
      on(event, callback) {
        const handle = addListener(name, event, callback);
        return () => void handle.remove();
      },
    }),
  };
}

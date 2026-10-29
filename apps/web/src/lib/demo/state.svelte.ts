/**
 * Whether this installation is the public demo (ADR 0065), from `/config.json`. The root layout
 * loads it once; the sign-in and registration pages read it.
 */

import { loadConfig, type DemoConfig } from '#lib/config.js';
import { sourceCodeUrl } from '#lib/legal/source.js';

class DemoState {
  /** The demo block of the config; null on every other installation. */
  config = $state<DemoConfig | null>(null);
  /** Where "Get MowDeck" leads: the source code of this build. */
  sourceUrl = $state('');
  loaded = $state(false);

  async load(): Promise<DemoConfig | null> {
    const runtime = await loadConfig();
    this.config = runtime.demo ?? null;
    this.sourceUrl = sourceCodeUrl(runtime.source);
    this.loaded = true;
    return this.config;
  }
}

export const demo = new DemoState();

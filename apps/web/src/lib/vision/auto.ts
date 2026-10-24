/**
 * Automatic AI analysis on errors (ADR 0035): what the switch on the AI provider page may do,
 * and how a proposal says that a mower error started it. The opt-in belongs to the user
 * (`auto_analysis` of `GET`/`PUT /api/v1/vision/me/preferences`); the installation can switch it off and
 * sets the cooldown and the daily limit per mower.
 */
import { m } from '#lib/paraglide/messages.js';

import type { Proposal, ProviderSettings, VisionPreferences } from './api';

/** `unavailable`: switched off by the operator; `no-provider`: nothing would answer. */
export type AutoAvailability = 'ready' | 'unavailable' | 'no-provider';

export function autoAvailability(
  prefs: VisionPreferences,
  settings: Pick<ProviderSettings, 'effective'> | null,
): AutoAvailability {
  if (!prefs.auto_analysis_available) return 'unavailable';
  if (!settings || settings.effective === 'none') return 'no-provider';
  return 'ready';
}

/**
 * Whether the switch can be used: always to switch off (a stored opt-in can be withdrawn even
 * when the provider went away), to switch on only when ready.
 */
export function canToggle(prefs: VisionPreferences, availability: AutoAvailability): boolean {
  return prefs.auto_analysis || availability === 'ready';
}

/** The limits line under the switch. */
export function limitsText(prefs: VisionPreferences): string {
  return m.ai_auto_limits({
    minutes: prefs.auto_cooldown_minutes,
    daily: prefs.auto_daily_per_mower,
  });
}

/** "Started automatically by error <code>" for a proposal an error started, else null. */
export function triggerNote(
  proposal: Pick<Proposal, 'trigger' | 'trigger_error_code'>,
): string | null {
  if (proposal.trigger !== 'error') return null;
  return m.vision_trigger_error({ code: proposal.trigger_error_code ?? '?' });
}

/**
 * Texts of the guided tour (ADR 0073). A step can have a demo note: what the public demo locks
 * there (ADR 0068) is explained, never triggered.
 */
import { m } from '#lib/paraglide/messages.js';

import type { TourContext, TourOutcome, TourStepId } from './steps';

export interface TourText {
  title: string;
  body: string;
  /** Shown in the public demo only. */
  demo: string | null;
}

export function tourText(
  id: TourStepId,
  context: Pick<TourContext, 'canManage' | 'demo' | 'sandbox'>,
): TourText {
  // Demo sandboxes (ADR 0076): the mowers are the visitor's own, not shared with everybody.
  const own = context.sandbox === true;
  const text = ((): TourText => {
    switch (id) {
      case 'welcome':
        return {
          title: m.tour_welcome_title(),
          body: m.tour_welcome_body(),
          demo: own ? m.tour_welcome_demo_sandbox() : m.tour_welcome_demo(),
        };
      case 'overview':
        return {
          title: m.tour_overview_title(),
          body: m.tour_overview_body(),
          demo: own ? m.tour_overview_demo_sandbox() : m.tour_overview_demo(),
        };
      case 'map':
        return { title: m.tour_map_title(), body: m.tour_map_body(), demo: null };
      case 'commands':
        return {
          title: m.tour_commands_title(),
          body: m.tour_commands_body(),
          demo: m.tour_commands_demo(),
        };
      case 'spot':
        return { title: m.tour_spot_title(), body: m.tour_spot_body(), demo: null };
      case 'zones':
        return { title: m.tour_zones_title(), body: m.tour_zones_body(), demo: null };
      case 'schedules':
        return { title: m.tour_schedules_title(), body: m.tour_schedules_body(), demo: null };
      case 'inbox':
        return {
          title: m.tour_inbox_title(),
          body: m.tour_inbox_body(),
          demo: m.tour_inbox_demo(),
        };
      case 'settings':
        return {
          title: m.tour_settings_title(),
          body: m.tour_settings_body(),
          demo: m.tour_settings_demo(),
        };
      case 'restart':
        return { title: m.tour_restart_title(), body: m.tour_restart_body(), demo: null };
      case 'add-mower':
        return context.canManage
          ? {
              title: m.tour_add_mower_title(),
              body: m.tour_add_mower_body(),
              demo: null,
            }
          : {
              title: m.tour_no_mower_title(),
              body: m.tour_no_mower_body(),
              demo: null,
            };
    }
  })();
  return context.demo ? text : { ...text, demo: null };
}

/** The last step leads into adding the first mower (instead of a plain "Done"). */
export function leadsToAddMower(
  id: TourStepId,
  context: Pick<TourContext, 'canManage' | 'demo'>,
): boolean {
  return id === 'add-mower' && context.canManage && !context.demo;
}

export function outcomeLabel(outcome: TourOutcome): string {
  switch (outcome) {
    case 'new':
      return m.tour_status_new();
    case 'seen':
      return m.tour_status_seen();
    case 'done':
      return m.tour_status_done();
    case 'skipped':
      return m.tour_status_skipped();
  }
}

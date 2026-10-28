/** Texts and links of the setup tour's steps. Every link leads into an existing flow. */
import { m } from '#lib/paraglide/messages.js';

import type { Facts, StepKey, StepState, TourStatus } from './steps';

export interface StepText {
  title: string;
  text: string;
  action: string;
}

export function stepText(key: StepKey): StepText {
  switch (key) {
    case 'cloud-account':
      return {
        title: m.onboarding_step_cloud_account_title(),
        text: m.onboarding_step_cloud_account_text(),
        action: m.onboarding_step_cloud_account_action(),
      };
    case 'mower':
      return {
        title: m.onboarding_step_mower_title(),
        text: m.onboarding_step_mower_text(),
        action: m.onboarding_step_mower_action(),
      };
    case 'map':
      return {
        title: m.onboarding_step_map_title(),
        text: m.onboarding_step_map_text(),
        action: m.onboarding_step_map_action(),
      };
    case 'notifications':
      return {
        title: m.onboarding_step_notifications_title(),
        text: m.onboarding_step_notifications_text(),
        action: m.onboarding_step_notifications_action(),
      };
    case 'passkey':
      return {
        title: m.onboarding_step_passkey_title(),
        text: m.onboarding_step_passkey_text(),
        action: m.onboarding_step_passkey_action(),
      };
    case 'schedule':
      return {
        title: m.onboarding_step_schedule_title(),
        text: m.onboarding_step_schedule_text(),
        action: m.onboarding_step_schedule_action(),
      };
    case 'sign-in-links':
      return {
        title: m.onboarding_step_sign_in_links_title(),
        text: m.onboarding_step_sign_in_links_text(),
        action: m.onboarding_step_sign_in_links_action(),
      };
    case 'app-extras':
      return {
        title: m.onboarding_step_app_extras_title(),
        text: m.onboarding_step_app_extras_text(),
        action: m.onboarding_step_app_extras_action(),
      };
  }
}

/** Where a step's action leads: the flow that already exists for it. */
export function stepHref(key: StepKey, facts: Pick<Facts, 'firstMowerId'>): string {
  switch (key) {
    case 'cloud-account':
      return '/settings#linked-accounts';
    case 'mower':
      return '/mowers/new';
    case 'map':
      return facts.firstMowerId ? `/mowers/${encodeURIComponent(facts.firstMowerId)}` : '/mowers';
    case 'notifications':
    case 'app-extras':
      return '/settings#notifications';
    case 'passkey':
    case 'sign-in-links':
      return '/settings#profile';
    case 'schedule':
      return '/schedules/new';
  }
}

export function stateLabel(state: StepState): string {
  switch (state) {
    case 'done':
      return m.onboarding_state_done();
    case 'skipped':
      return m.onboarding_state_skipped();
    case 'open':
      return m.onboarding_state_open();
  }
}

export function statusLabel(status: TourStatus): string {
  switch (status) {
    case 'new':
      return m.onboarding_section_status_new();
    case 'active':
      return m.onboarding_section_status_active();
    case 'dismissed':
      return m.onboarding_section_status_dismissed();
    case 'completed':
      return m.onboarding_section_status_completed();
  }
}

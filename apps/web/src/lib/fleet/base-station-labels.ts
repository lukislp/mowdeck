/** Words of the base station page (de and en through Paraglide), exhaustive per enum. */
import type { ApiProblem } from '#lib/api/problem.js';
import { problemMessage } from '#lib/i18n.js';
import { m } from '#lib/paraglide/messages.js';

import type { BaseStationState, BaseUpdateBlocker, FirmwareInfo } from './base-stations';

export function correctionLabel(link: BaseStationState['correction_link']): string {
  switch (link) {
    case 'lora':
      return m.bs_link_lora();
    case 'internet':
      return m.bs_link_internet();
    case 'network':
      return m.bs_link_network();
    default:
      return m.bs_link_unknown();
  }
}

export function solutionLabel(solution: BaseStationState['rtk_status']): string {
  switch (solution) {
    case 'fixed':
      return m.bs_solution_fixed();
    case 'float':
      return m.bs_solution_float();
    case 'single':
      return m.bs_solution_single();
    case 'none':
      return m.bs_solution_none();
    default:
      return m.bs_solution_unknown();
  }
}

export function firmwareStateLabel(state: FirmwareInfo['update_state'] | undefined): string {
  switch (state) {
    case 'idle':
      return m.bs_fw_state_idle();
    case 'available':
      return m.bs_fw_state_available();
    case 'installing':
      return m.bs_fw_state_installing();
    case 'failed':
      return m.bs_fw_state_failed();
    default:
      return m.bs_fw_state_unknown();
  }
}

export function baseUpdateBlockerText(blocker: BaseUpdateBlocker): string {
  switch (blocker) {
    case 'no_permission':
      return m.bs_fw_block_no_permission();
    case 'plan':
      return m.bs_fw_block_plan();
    case 'no_mower':
      return m.bs_fw_block_no_mower();
    case 'offline':
      return m.bs_fw_block_offline();
    case 'unsupported':
      return m.bs_fw_block_unsupported();
    case 'no_check':
      return m.bs_fw_block_no_check();
    case 'installing':
      return m.bs_fw_block_installing();
    case 'no_update':
      return m.bs_fw_block_no_update();
    case 'mower_busy':
      return m.bs_fw_block_mower_busy();
    case 'drive_session':
      return m.bs_fw_block_drive_session();
  }
}

/** Fleet's base station refusals in words; any other problem as usual. */
export function baseStationProblemText(problem: ApiProblem): string {
  switch (problem.slug) {
    case 'base-station-not-applicable':
      return m.bs_not_applicable();
    case 'base-station-already-registered':
      return m.bs_already_registered();
    case 'base-station-not-linked':
      return m.bs_not_linked();
    default:
      return problemMessage(problem);
  }
}

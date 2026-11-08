// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * Gathers what "Report a problem" tells about the running app, from the runtime config and the
 * browser. Kept thin: everything is injected, the parsing and the address are in
 * `report-problem.ts`. Environment data only (see the privacy note there).
 */
import type { SourceInfo } from '#lib/legal/source.js';
import { displayModel } from '#lib/mower/limits.js';

import { parseUserAgent, type ClientPlatform, type ReportProblemInput } from './report-problem';

export interface EnvironmentDeps {
  source: SourceInfo;
  userAgent: string;
  /** The native shell's platform, null in a browser. */
  shellPlatform: ClientPlatform | null;
  locale: string;
  demo: boolean;
  /** The first mower the user can see, reduced to what the form needs. */
  mower?: { model: string | null; connection_kind: string | null } | null;
}

/**
 * The first mower for the form, with the model the mower list shows: the model its capabilities
 * report (`LUBA 2 AWD 3000X`), else the one registered in Fleet. Fleet's value is what the
 * vendor's device list carried at linking time and can be a series or a name the owner gave
 * (`Luba VP`), so it is only the fallback.
 */
export function firstMower(
  mowers: readonly { id: string; model?: string | null; connection_kind: string }[],
  capabilities: readonly { mower_id: string; capabilities: { model?: string | null } | null }[],
): EnvironmentDeps['mower'] {
  const first = mowers[0];
  if (!first) return null;
  const caps = capabilities.find((item) => String(item.mower_id) === String(first.id));
  return {
    model: displayModel(caps?.capabilities ?? null, first),
    connection_kind: first.connection_kind,
  };
}

export function gatherEnvironment(deps: EnvironmentDeps): ReportProblemInput {
  const { browser, os } = parseUserAgent(deps.userAgent);
  return {
    source: deps.source,
    platform: deps.shellPlatform ?? 'web',
    browser,
    os,
    locale: deps.locale,
    demo: deps.demo,
    mower: deps.mower ? { model: deps.mower.model, link: deps.mower.connection_kind } : null,
  };
}

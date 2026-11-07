// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * Gathers what "Report a problem" tells about the running app, from the runtime config and the
 * browser. Kept thin: everything is injected, the parsing and the address are in
 * `report-problem.ts`. Environment data only (see the privacy note there).
 */
import type { SourceInfo } from '#lib/legal/source.js';

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

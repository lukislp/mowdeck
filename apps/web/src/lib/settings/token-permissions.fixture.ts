/** A permission catalogue as Identity serves it, for the token tests. */
import type { CatalogueEntry } from './token-permissions';

export function entry(key: string, extra: Partial<CatalogueEntry> = {}): CatalogueEntry {
  const inModel = !extra.implied_by;
  return {
    key,
    description: `${key} (catalogue)`,
    module: 'test',
    levels: inModel ? ['account', 'site', 'group', 'mower'] : ['account'],
    in_model: inModel,
    api_tokens: true,
    implied_by: [],
    ...extra,
  };
}

/** The catalogue of a current installation, in Identity's (alphabetical) order. */
export const CATALOGUE: CatalogueEntry[] = [
  entry('audit.view', { levels: ['account'], api_tokens: false }),
  entry('billing.manage', { levels: ['account'], api_tokens: false }),
  entry('device_gateway.view', { implied_by: ['mower.view'] }),
  entry('fleet.bulk_command', { levels: ['account', 'site', 'group'] }),
  entry('fleet.manage'),
  entry('mower.control'),
  entry('mower.drive'),
  entry('mower.view'),
  entry('notifications.view', { implied_by: ['mower.view'] }),
  entry('schedule.edit'),
  entry('scheduler.view', { implied_by: ['mower.view'] }),
  entry('site.only', { levels: ['site'] }),
  entry('telemetry.view', { implied_by: ['mower.view'] }),
  entry('users.manage', { levels: ['account'], api_tokens: false }),
  entry('vendor.thing', { implied_by: ['mower.view', 'mower.control'] }),
];

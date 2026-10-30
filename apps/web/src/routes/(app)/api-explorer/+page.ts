// The API explorer (ADR 0072) exists only where /config.json turns it on
// (MOWDECK_WEB_API_EXPLORER=true, the public demo): everywhere else the route is a 404.
import { error } from '@sveltejs/kit';

import { loadConfig } from '#lib/config.js';

export async function load() {
  const config = await loadConfig();
  if (!config.apiExplorer) error(404, 'Not found');
  return { resetHours: config.demo?.resetHours ?? null };
}

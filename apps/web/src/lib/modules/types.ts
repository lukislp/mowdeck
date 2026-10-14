import type { IconName } from '@mowdeck/ui';

/** A built-in route module of the shell (design 3.1). */
export interface AppModule {
  id: string;
  /** Route of the module; must match a SvelteKit route and, unless `core`, a ui_area. */
  route: string;
  /** Returns the translated menu label (a Paraglide message). */
  label: () => string;
  icon: IconName;
  /** Permission key from `me.permissions` that shows the module. */
  requiredPermission: string | null;
  order: number;
  /** Shown without a ui_area (the account page). */
  core?: boolean;
  /** Only for operator admins of the installation (`platform:main`); there is no permission key
   * for it in `me.permissions`, the shell probes the operator endpoint once instead. */
  platformAdmin?: boolean;
  /** Where the module lives: an entry of the navigation (default) or a section of the one
   * settings page (`/settings`), whose old route then forwards to the section. */
  placement?: 'primary' | 'settings';
  /** Anchor id of the section on the settings page (placement `settings`). */
  section?: string;
  /** The ui_area route that enables a non-core module, when it differs from `route` (a section
   * served inside another service's area). */
  area?: string;
}

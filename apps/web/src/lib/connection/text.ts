/**
 * Translated texts of the connection forms. The forms come from Fleet's schemas; this file only
 * names what the app has translations for (the kinds and options of the Mammotion adapters).
 * Anything else falls back to the schema's English `title` and `description`, so a new adapter
 * kind renders without a change here.
 */
import { connectionLabel } from '#lib/i18n.js';
import { m } from '#lib/paraglide/messages.js';

import type { FieldIssue, KindSchema, OptionSchema } from './form';

type Text = () => string;

interface OptionText {
  label: Text;
  help?: Text;
  /** Readable format for pattern errors. */
  format?: Text;
}

const REF: OptionText = {
  label: m.conn_credential_ref,
  help: m.conn_credential_ref_help,
  format: m.conn_credential_ref_format,
};

const TIMEOUTS: Record<string, OptionText> = {
  map_sync_timeout_s: { label: m.conn_map_sync_timeout, help: m.conn_map_sync_timeout_help },
  confirm_timeout_s: { label: m.conn_confirm_timeout, help: m.conn_confirm_timeout_help },
};

const OPTIONS: Record<string, Record<string, OptionText>> = {
  cloud: {
    account: { label: m.conn_cloud_account, help: m.conn_cloud_account_help },
    password: { label: m.conn_cloud_password, help: m.conn_cloud_password_help },
    credential_ref: REF,
    device_name: { label: m.conn_cloud_device_name, help: m.conn_cloud_device_name_help },
    iot_id: { label: m.conn_cloud_iot_id, help: m.conn_cloud_iot_id_help },
    session_cache_path: {
      label: m.conn_cloud_session_cache,
      help: m.conn_cloud_session_cache_help,
    },
    refresh_seconds: { label: m.conn_cloud_refresh, help: m.conn_cloud_refresh_help },
    refresh_seconds_active: {
      label: m.conn_cloud_refresh_active,
      help: m.conn_cloud_refresh_active_help,
    },
    report_stream_seconds: {
      label: m.conn_cloud_report_stream,
      help: m.conn_cloud_report_stream_help,
    },
    cloud_drive: { label: m.conn_cloud_drive, help: m.conn_cloud_drive_help },
    allow_manual_blades: { label: m.conn_ble_manual_blades, help: m.conn_ble_manual_blades_help },
    manual_blade_height_mm: { label: m.conn_ble_manual_blade_height },
    manual_drive_speed_mps: {
      label: m.conn_ble_manual_drive_speed,
      help: m.conn_ble_manual_drive_speed_help,
    },
    ...TIMEOUTS,
  },
  ble_bridge: {
    host: { label: m.conn_ble_host, help: m.conn_ble_host_help },
    port: { label: m.conn_ble_port, help: m.conn_ble_port_help },
    noise_psk: { label: m.conn_ble_noise_psk, help: m.conn_ble_noise_psk_help },
    credential_ref: { ...REF, help: m.conn_ble_key_ref_help },
    ble_address: {
      label: m.conn_ble_address,
      help: m.conn_ble_address_help,
      format: m.conn_ble_address_format,
    },
    device_name: { label: m.conn_ble_device_name, help: m.conn_ble_device_name_help },
    allow_manual_blades: { label: m.conn_ble_manual_blades, help: m.conn_ble_manual_blades_help },
    manual_blade_height_mm: { label: m.conn_ble_manual_blade_height },
    manual_drive_speed_mps: {
      label: m.conn_ble_manual_drive_speed,
      help: m.conn_ble_manual_drive_speed_help,
    },
    scan_timeout_s: { label: m.conn_ble_scan_timeout, help: m.conn_ble_scan_timeout_help },
    ...TIMEOUTS,
  },
};

const GROUPS: Record<string, Record<string, Text>> = {
  cloud: { device: m.conn_group_device, credential: m.conn_group_login },
  ble_bridge: { credential: m.conn_group_key },
};

/** The kind's name: translated for the known kinds, the schema's title otherwise. */
export function kindTitle(schema: KindSchema): string {
  const known = connectionLabel(schema.kind);
  return known === schema.kind ? schema.title : known;
}

export function kindDescription(schema: KindSchema): string {
  switch (schema.kind) {
    case 'simulated':
      return m.conn_simulated_description();
    case 'cloud':
      return m.conn_cloud_description();
    case 'ble_bridge':
      return m.conn_ble_description();
    default:
      return schema.description;
  }
}

export function optionLabel(kind: string, option: OptionSchema): string {
  return OPTIONS[kind]?.[option.key]?.label() ?? option.title;
}

export function optionHelp(kind: string, option: OptionSchema): string {
  const own = OPTIONS[kind]?.[option.key];
  if (own) return own.help?.() ?? '';
  return option.description;
}

export function groupLabel(schema: KindSchema, group: string): string {
  return (
    GROUPS[schema.kind]?.[group]?.() ?? schema.groups.find((g) => g.key === group)?.title ?? group
  );
}

function numberText(value: number | null): string {
  return value === null ? '' : String(value);
}

/** The message of a validation issue for `option`. */
export function issueMessage(kind: string, option: OptionSchema, issue: FieldIssue): string {
  switch (issue.code) {
    case 'required':
      return m.conn_error_required();
    case 'pattern':
      return m.conn_error_pattern({
        format:
          OPTIONS[kind]?.[option.key]?.format?.() ?? option.format_hint ?? option.pattern ?? '',
      });
    case 'range':
      return m.conn_error_range({
        min: numberText(option.minimum),
        max: numberText(option.maximum),
      });
    case 'integer':
      return m.conn_error_integer();
    case 'number':
      return m.conn_error_number();
    case 'server':
      return issue.message ?? m.problem_validation();
  }
}

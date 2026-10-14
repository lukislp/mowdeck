/**
 * Notification kinds and channels in the app: translated names (exhaustive, so a new contract
 * kind fails the type check) and the state tone of each kind's badge (colour means state:
 * stuck is the error tone, battery low and offline the warning and offline tones). A
 * `vision_proposal` (AI assist waits for approval) links to `/mowers/<id>?assist=<proposal>`,
 * and so does `vision_auto_proposal` (the same, started automatically by a mower error).
 */
import type { components } from '$lib/generated/notifications';
import { m } from '$lib/paraglide/messages';

export type NotificationKind = components['schemas']['NotificationKind'];
export type NotificationTone = 'mowing' | 'docked' | 'paused' | 'error' | 'offline' | 'unknown';

/** The kinds a user can switch on, in the order of the settings page. */
export const USER_KINDS: NotificationKind[] = [
  'mowing_finished',
  'mower_stuck',
  'battery_low',
  'mower_offline',
  'mower_online',
  'job_started',
  'mower_lifted',
  'mower_left_dock_unexpectedly',
  'geofence_exit',
  'blade_maintenance',
  'vision_proposal',
  'vision_auto_proposal',
];

/** Theft alarms: critical, acknowledged by the user in the inbox (ADR 0027). */
export const ALARM_KINDS: ReadonlySet<NotificationKind> = new Set<NotificationKind>([
  'mower_lifted',
  'mower_left_dock_unexpectedly',
  'geofence_exit',
]);

export function isAlarm(kind: NotificationKind): boolean {
  return ALARM_KINDS.has(kind);
}

/** The reported position in an alarm link (`/mowers/<id>/map?lat=..&lon=..`), if any. */
export function alarmPosition(url: string | null | undefined): { lat: string; lon: string } | null {
  if (!url) return null;
  try {
    const params = new URL(url, 'http://app.invalid').searchParams;
    const lat = Number(params.get('lat'));
    const lon = Number(params.get('lon'));
    if (!params.has('lat') || !params.has('lon') || !Number.isFinite(lat) || !Number.isFinite(lon))
      return null;
    if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return null;
    return { lat: lat.toFixed(6), lon: lon.toFixed(6) };
  } catch {
    return null;
  }
}

export function kindLabel(kind: NotificationKind): string {
  switch (kind) {
    case 'mowing_finished':
      return m.notif_kind_mowing_finished();
    case 'mower_stuck':
      return m.notif_kind_mower_stuck();
    case 'battery_low':
      return m.notif_kind_battery_low();
    case 'mower_offline':
      return m.notif_kind_mower_offline();
    case 'mower_online':
      return m.notif_kind_mower_online();
    case 'job_started':
      return m.notif_kind_job_started();
    case 'mower_lifted':
      return m.notif_kind_mower_lifted();
    case 'mower_left_dock_unexpectedly':
      return m.notif_kind_mower_left_dock_unexpectedly();
    case 'geofence_exit':
      return m.notif_kind_geofence_exit();
    case 'blade_maintenance':
      return m.notif_kind_blade_maintenance();
    case 'vision_proposal':
      return m.notif_kind_vision_proposal();
    case 'vision_auto_proposal':
      return m.notif_kind_vision_auto_proposal();
    case 'test':
      return m.notif_kind_test();
  }
}

export function kindHint(kind: NotificationKind): string {
  switch (kind) {
    case 'mowing_finished':
      return m.notif_kind_mowing_finished_hint();
    case 'mower_stuck':
      return m.notif_kind_mower_stuck_hint();
    case 'battery_low':
      return m.notif_kind_battery_low_hint();
    case 'mower_offline':
      return m.notif_kind_mower_offline_hint();
    case 'mower_online':
      return m.notif_kind_mower_online_hint();
    case 'job_started':
      return m.notif_kind_job_started_hint();
    case 'mower_lifted':
      return m.notif_kind_mower_lifted_hint();
    case 'mower_left_dock_unexpectedly':
      return m.notif_kind_mower_left_dock_unexpectedly_hint();
    case 'geofence_exit':
      return m.notif_kind_geofence_exit_hint();
    case 'blade_maintenance':
      return m.notif_kind_blade_maintenance_hint();
    case 'vision_proposal':
      return m.notif_kind_vision_proposal_hint();
    case 'vision_auto_proposal':
      return m.notif_kind_vision_auto_proposal_hint();
    case 'test':
      return '';
  }
}

export function kindTone(kind: NotificationKind): NotificationTone {
  switch (kind) {
    case 'mowing_finished':
      return 'docked';
    case 'job_started':
    case 'mower_online':
      return 'mowing';
    case 'mower_stuck':
    case 'mower_lifted':
    case 'mower_left_dock_unexpectedly':
    case 'geofence_exit':
      return 'error';
    case 'battery_low':
    case 'blade_maintenance':
    case 'vision_proposal':
    case 'vision_auto_proposal':
      return 'paused';
    case 'mower_offline':
      return 'offline';
    case 'test':
      return 'unknown';
  }
}

export function channelLabel(channel: string): string {
  switch (channel) {
    case 'webpush':
      return m.notif_channel_webpush();
    case 'ntfy':
      return m.notif_channel_ntfy();
    case 'apns':
      return m.notif_channel_apns();
    case 'log':
      return m.notif_channel_log();
    default:
      return channel;
  }
}

export function channelHint(channel: string): string | undefined {
  switch (channel) {
    case 'webpush':
      return m.notif_channel_webpush_hint();
    case 'ntfy':
      return m.notif_channel_ntfy_hint();
    case 'apns':
      return m.notif_channel_apns_hint();
    case 'log':
      return m.notif_channel_log_hint();
    default:
      return undefined;
  }
}

/** The proposal id in a `vision_proposal` or `vision_auto_proposal` link (`/mowers/<id>?assist=<proposal>`), if any. */
export function assistProposal(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const value = new URL(url, 'http://app.invalid').searchParams.get('assist');
    return value && /^[0-9a-f-]{8,64}$/i.test(value) ? value : null;
  } catch {
    return null;
  }
}

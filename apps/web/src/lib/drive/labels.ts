/**
 * Texts of the drive page: why a session ended and why a start was refused. Exhaustive switches,
 * so a new reason or error code in the Realtime contract fails the type check.
 */
import type { RealtimeErrorCode } from '#lib/generated/realtime.js';
import { m } from '#lib/paraglide/messages.js';
import type { DriveEnd } from '#lib/realtime/client.svelte.js';

export function driveEndLabel(reason: DriveEnd): string {
  switch (reason) {
    case 'client_stop':
      return m.drive_ended_client_stop();
    case 'socket_closed':
      return m.drive_ended_socket_closed();
    case 'revoked':
      return m.drive_ended_revoked();
    case 'token_expired':
      return m.drive_ended_token_expired();
    case 'max_duration':
      return m.drive_ended_max_duration();
    case 'mower_refused':
      return m.drive_ended_mower_refused();
    case 'mower_busy':
      return m.drive_ended_mower_busy();
    case 'replaced':
      return m.drive_ended_replaced();
    case 'expired':
      return m.drive_ended_expired();
    case 'connection_lost':
      return m.drive_ended_connection_lost();
    case 'completed':
      return m.drive_ended_completed();
    case 'aborted':
      return m.drive_ended_aborted();
  }
}

export function driveErrorLabel(code: RealtimeErrorCode): string {
  switch (code) {
    case 'drive_not_allowed':
      return m.drive_error_not_allowed();
    case 'drive_unsupported':
      return m.drive_error_unsupported();
    case 'drive_busy':
      return m.drive_error_busy();
    case 'drive_unavailable':
      return m.drive_error_unavailable();
    case 'drive_not_active':
      return m.drive_error_not_active();
    case 'invalid_message':
    case 'unauthorized':
    case 'limit_exceeded':
    case 'internal':
      return m.drive_error_other();
  }
}

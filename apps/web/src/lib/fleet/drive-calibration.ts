/**
 * Fleet's drive calibration of a mower (ADR 0042): `PUT` stores a measured fit, `DELETE` removes
 * it (spot runs then use the default values), and `MowerOut.drive_calibration` shows the stored
 * one. Permission `can_drive`.
 */
import { fleetApi, unwrap } from '$lib/api/client';
import type { DriveCalibration, DriveCalibrationIn } from '$lib/mower/drive-calibration';
import { storedCalibrationOf } from '$lib/mower/drive-calibration';

const path = (mowerId: string) => ({ params: { path: { mower_id: mowerId } } });

export async function putDriveCalibration(
  mowerId: string,
  input: DriveCalibrationIn,
): Promise<DriveCalibration> {
  const saved = await unwrap(
    fleetApi.PUT('/api/v1/fleet/mowers/{mower_id}/drive-calibration', {
      ...path(mowerId),
      body: input,
    }),
  );
  return storedCalibrationOf({ drive_calibration: saved }) ?? { ...input };
}

export async function deleteDriveCalibration(mowerId: string): Promise<void> {
  const result = await fleetApi.DELETE(
    '/api/v1/fleet/mowers/{mower_id}/drive-calibration',
    path(mowerId),
  );
  if (result.response.status === 404) return;
  await unwrap(Promise.resolve(result));
}

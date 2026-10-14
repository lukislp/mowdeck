/**
 * The calibration methods with their names (the one place that knows all of them): `manual`,
 * `features` (falling back to `correlation`) and `ai` on a points source.
 */
import { m } from '$lib/paraglide/messages';

import { aiMethod, type CorrespondenceSource } from './ai';
import { correlationMethod } from './correlation';
import { featuresMethod } from './local-features';
import { manualMethod } from './manual';
import type { CalibrationMethod, CalibrationMethodId } from './types';

export type CalibrationMethods = Record<CalibrationMethodId, CalibrationMethod>;

/** The name of a method in the UI. */
export function methodLabel(id: CalibrationMethodId): string {
  switch (id) {
    case 'manual':
      return m.camera_calibration_method_manual();
    case 'features':
      return m.camera_calibration_method_features();
    case 'ai':
      return m.camera_calibration_method_ai();
    case 'correlation':
      return m.camera_calibration_method_correlation();
  }
}

/** Every method; `ai` asks `correspondences` for its points. */
export function calibrationMethods(correspondences: CorrespondenceSource): CalibrationMethods {
  const correlation = correlationMethod(() => methodLabel('correlation'));
  return {
    manual: manualMethod(() => methodLabel('manual')),
    correlation,
    features: featuresMethod({ label: () => methodLabel('features'), fallback: correlation }),
    ai: aiMethod(() => methodLabel('ai'), correspondences),
  };
}

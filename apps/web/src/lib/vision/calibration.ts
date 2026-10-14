/**
 * The Vision service's part of the AI-assisted panorama calibration (ADR 0037):
 * `POST /api/v1/vision/calibration/correspondences` with a left and a right JPEG of the same
 * moment, answered with corresponding points. Implements the camera calibration's
 * {@link CorrespondenceSource} port, so the calibration code never sees the HTTP client; Vision
 * problems become {@link CalibrationError}s the card can explain (provider missing, limit,
 * plan).
 */
import type { Client } from 'openapi-fetch';

import { unwrap, visionModelApi } from '$lib/api/client';
import { ApiProblem, PROBLEM_PREFIX } from '$lib/api/problem';
import type { CorrespondenceSource } from '$lib/camera/calibration/ai';
import { CalibrationError, type CalibrationFailure } from '$lib/camera/calibration/types';
import type { paths } from '$lib/generated/vision';

import { VISION_PROBLEMS } from './api';

/** The slug of {@link ApiProblem.network}: no answer within the deadline, or no connection. */
const NETWORK_SLUG = ApiProblem.network().type.slice(PROBLEM_PREFIX.length);

/** The calibration failure a Vision problem stands for. */
export function failureOf(problem: ApiProblem): CalibrationFailure {
  switch (problem.slug) {
    case VISION_PROBLEMS.providerMissing:
      return 'provider_missing';
    case VISION_PROBLEMS.quota:
      return 'quota';
    case VISION_PROBLEMS.notInPlan:
      return 'not_in_plan';
    // Vision's own time budget ran out, or the browser's deadline: the same advice (try again).
    case VISION_PROBLEMS.providerTimeout:
    case NETWORK_SLUG:
      return 'unreachable';
    default:
      return 'failed';
  }
}

/**
 * The points source of one mower on `client` (default: the app's Vision client with the
 * model-call deadline, looked up only when the AI method actually runs). An aborted `signal`
 * (the user discarded the run) cancels the request; the rejection is the fetch's abort error,
 * not a {@link CalibrationError}.
 */
export function visionCorrespondences(
  mowerId: string,
  client?: Client<paths>,
): CorrespondenceSource {
  return {
    async correspondences(left, right, signal) {
      try {
        const answer = await unwrap(
          (client ?? visionModelApi).POST('/api/v1/vision/calibration/correspondences', {
            signal,
            body: {
              mower_id: mowerId,
              left: {
                media_type: 'image/jpeg',
                data: left.data,
                side: 'left',
                captured_at: new Date(left.capturedAt).toISOString(),
              },
              right: {
                media_type: 'image/jpeg',
                data: right.data,
                side: 'right',
                captured_at: new Date(right.capturedAt).toISOString(),
              },
            },
          }),
        );
        return {
          points: answer.points.map((p) => ({
            left: [p.left[0] ?? 0, p.left[1] ?? 0] as const,
            right: [p.right[0] ?? 0, p.right[1] ?? 0] as const,
            label: p.label,
          })),
          leftSize: answer.left_size,
          rightSize: answer.right_size,
          model: answer.model,
          costUsd: answer.cost.cost_usd ?? null,
        };
      } catch (error) {
        if (error instanceof ApiProblem) {
          throw new CalibrationError(failureOf(error), error.detail ?? error.title);
        }
        throw error;
      }
    },
  };
}

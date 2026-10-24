/** Texts for the problems of the Vision service (title and an optional detail). */
import type { ApiProblem } from '#lib/api/problem.js';
import { problemMessage } from '#lib/i18n.js';
import { m } from '#lib/paraglide/messages.js';
import { planProblemText } from '#lib/plan/problems.js';

import { VISION_PROBLEMS } from './api';

export function visionProblemText(problem: ApiProblem): { title: string; detail: string | null } {
  switch (problem.slug) {
    case VISION_PROBLEMS.providerMissing:
      return { title: m.vision_problem_provider_missing(), detail: m.vision_provider_none_hint() };
    case VISION_PROBLEMS.quota:
      return { title: m.vision_problem_quota(), detail: problem.detail };
    case VISION_PROBLEMS.secretsUnavailable:
      return { title: m.vision_problem_secrets(), detail: m.ai_provider_no_secrets() };
    case VISION_PROBLEMS.expired:
      return { title: m.vision_problem_expired(), detail: null };
    case VISION_PROBLEMS.providerFailed:
      return { title: m.vision_problem_provider_failed(), detail: problem.detail };
    case VISION_PROBLEMS.notInPlan: {
      const plan = planProblemText(problem);
      if (plan) return plan;
      break;
    }
    default:
      break;
  }
  return { title: problemMessage(problem), detail: problem.detail };
}

/**
 * An automatic analysis that got no camera photo (live case 2026-10-24: weak Wi-Fi, -79 dBm),
 * as the Vision API reports it. Real failures need a mower on a weak link, which the test stack
 * cannot produce, so the specs answer `GET /api/v1/vision/proposals/<id>` with this proposal
 * and open it by its link, like the notification does.
 */
import type { Page } from '@playwright/test';

export function noPhotoProposal(mowerId: string, proposalId: string): Record<string, unknown> {
  const at = new Date().toISOString();
  return {
    id: proposalId,
    chain_id: proposalId,
    round: 1,
    mower_id: mowerId,
    task: 'recover',
    state: 'failed',
    final: true,
    requested_by: { kind: 'user', subject: '00000000-0000-4000-8000-000000000001' },
    created_at: at,
    updated_at: at,
    provider_model: null,
    provider_family: null,
    hosted: false,
    assessment: null,
    action: 'none',
    fallback: false,
    job: null,
    steps: [],
    image_count: 0,
    light_used: false,
    stereo: false,
    frames: [],
    trigger: 'error',
    trigger_error_code: '1201',
    photo_problem: 'weak_wifi',
    signal_dbm: -79,
    detail:
      'no photo after 3 attempts: weak Wi-Fi (-79 dBm), the camera stream did not get through; ' +
      'start the analysis by hand once the connection is better',
    analysed_at: null,
    expires_at: null,
    approved_by: null,
    approved_at: null,
    decided_by: null,
    decided_at: null,
    started_at: null,
    finished_at: null,
    drive_session_id: null,
    return_command_id: null,
    can_retry: false,
    rounds_left: 2,
  };
}

/** Answer the proposal `proposalId` with the failed automatic analysis without photo. */
export async function routeNoPhoto(page: Page, mowerId: string, proposalId: string): Promise<void> {
  await page.route(`**/api/v1/vision/proposals/${proposalId}`, async (route) => {
    await route.fulfill({ json: noPhotoProposal(mowerId, proposalId) });
  });
}

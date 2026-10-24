/**
 * AI assist (Vision service, `/api/v1/vision`): typed calls of the openapi-fetch client. Every
 * call returns the data or throws the {@link ApiProblem}; the store and the pages take a
 * {@link VisionApi} so tests can hand in a client with a fake fetch.
 */
import type { Client } from 'openapi-fetch';

import { unwrap, visionApi } from '$lib/api/client';
import type { ApiProblem } from '$lib/api/problem';
import type { components, paths } from '$lib/generated/vision';

type Schemas = components['schemas'];

export type VisionStatus = Schemas['StatusOut'];
export type ProviderFamily = Schemas['FamilyOut'];
export type ProviderCatalogue = Schemas['ProvidersOut'];
export type ProviderSettings = Schemas['ProviderOut'];
export type ProviderInput = Schemas['ProviderIn'];
export type ProviderTest = Schemas['ProviderTestOut'];
export type Proposal = Schemas['ProposalOut'];
export type ProposalState = Schemas['ProposalState'];
export type ProposalStep = Schemas['StepOut'];
export type ProposalOption = Schemas['OptionOut'];
export type ApprovalOption = ProposalOption['option'];
export type StepOutcome = Schemas['StepOutcome'];
export type RiskFlag = Schemas['RiskFlag'];
export type VisionTask = Schemas['VisionTask'];
export type VisionImage = Schemas['ImageIn'];
export type ProposalFrame = Schemas['FrameOut'];
export type ProposalJob = Schemas['JobOut'];
export type JobArea = Schemas['JobAreaOut'];
export type VisionTrigger = Schemas['VisionTrigger'];
export type VisionPreferences = Schemas['PreferencesOut'];
export type VisionPreferencesInput = Schemas['PreferencesIn'];

/** Problem types of the Vision service the app reacts to (the part after the URN prefix). */
export const VISION_PROBLEMS = {
  providerMissing: 'vision-provider-missing',
  secretsUnavailable: 'vision-secrets-unavailable',
  quota: 'vision-quota',
  expired: 'vision-proposal-expired',
  providerFailed: 'vision-provider-failed',
  notInPlan: 'feature-not-in-plan',
  conflict: 'conflict',
} as const;

/** The settings page of the user's own provider (linked when none is configured). */
export const PROVIDER_SETTINGS_ROUTE = '/settings#ai-provider';

export interface VisionApi {
  status(): Promise<VisionStatus>;
  providers(): Promise<ProviderCatalogue>;
  provider(): Promise<ProviderSettings>;
  saveProvider(body: ProviderInput): Promise<ProviderSettings>;
  deleteProvider(): Promise<void>;
  testProvider(): Promise<ProviderTest>;
  /** The caller's AI assist preferences (`stereo_frames`, the opt-in `auto_analysis`). */
  preferences(): Promise<VisionPreferences>;
  /** Change preferences; fields left out keep their value. */
  savePreferences(body: VisionPreferencesInput): Promise<VisionPreferences>;
  recover(mowerId: string, task: VisionTask, images: VisionImage[]): Promise<Proposal>;
  proposals(mowerId: string, limit?: number): Promise<Proposal[]>;
  proposal(proposalId: string): Promise<Proposal>;
  /** Approve; `option` picks `model` or `fallback` when the proposal offers both. */
  approve(proposalId: string, option?: ApprovalOption): Promise<Proposal>;
  reject(proposalId: string): Promise<Proposal>;
  retry(proposalId: string, images: VisionImage[]): Promise<Proposal>;
  /** The stored snapshot `index` of a proposal (short lived on the server). */
  image(proposalId: string, index: number): Promise<Blob>;
}

/** The calls on `client` (default: the app's authenticated Vision client). */
export function visionCalls(client: Client<paths> = visionApi): VisionApi {
  const proposalPath = (proposalId: string) => ({ params: { path: { proposal_id: proposalId } } });
  return {
    status: () => unwrap(client.GET('/api/v1/vision/status')),
    providers: () => unwrap(client.GET('/api/v1/vision/providers')),
    provider: () => unwrap(client.GET('/api/v1/vision/me/provider')),
    saveProvider: (body) => unwrap(client.PUT('/api/v1/vision/me/provider', { body })),
    deleteProvider: async () => {
      await unwrap(client.DELETE('/api/v1/vision/me/provider'));
    },
    testProvider: () => unwrap(client.POST('/api/v1/vision/me/provider/test')),
    preferences: () => unwrap(client.GET('/api/v1/vision/me/preferences')),
    savePreferences: (body) => unwrap(client.PUT('/api/v1/vision/me/preferences', { body })),
    recover: (mowerId, task, images) =>
      unwrap(
        client.POST('/api/v1/vision/mowers/{mower_id}/recover', {
          params: { path: { mower_id: mowerId } },
          body: { task, images },
        }),
      ),
    proposals: async (mowerId, limit = 5) =>
      (
        await unwrap(
          client.GET('/api/v1/vision/mowers/{mower_id}/proposals', {
            params: { path: { mower_id: mowerId }, query: { limit } },
          }),
        )
      ).items,
    proposal: (proposalId) =>
      unwrap(client.GET('/api/v1/vision/proposals/{proposal_id}', proposalPath(proposalId))),
    approve: (proposalId, option) =>
      unwrap(
        client.POST(
          '/api/v1/vision/proposals/{proposal_id}/approve',
          option ? { ...proposalPath(proposalId), body: { option } } : proposalPath(proposalId),
        ),
      ),
    reject: (proposalId) =>
      unwrap(
        client.POST('/api/v1/vision/proposals/{proposal_id}/reject', proposalPath(proposalId)),
      ),
    retry: (proposalId, images) =>
      unwrap(
        client.POST('/api/v1/vision/proposals/{proposal_id}/retry', {
          ...proposalPath(proposalId),
          body: { images },
        }),
      ),
    image: (proposalId, index) =>
      unwrap(
        client.GET('/api/v1/vision/proposals/{proposal_id}/images/{index}', {
          params: { path: { proposal_id: proposalId, index } },
          parseAs: 'blob',
        }),
      ) as Promise<Blob>,
  };
}

/** True when `problem` is the Vision problem `slug` (see {@link VISION_PROBLEMS}). */
export function isVisionProblem(problem: ApiProblem | null, slug: string): boolean {
  return !!problem && problem.slug === slug;
}

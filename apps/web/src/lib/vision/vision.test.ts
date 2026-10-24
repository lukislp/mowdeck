import createClient from 'openapi-fetch';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiProblem } from '$lib/api/problem';
import type { paths } from '$lib/generated/vision';
import { m } from '$lib/paraglide/messages';

import {
  isVisionProblem,
  visionCalls,
  type Proposal,
  type ProviderFamily,
  type ProviderSettings,
  type VisionApi,
} from './api';
import { AssistStore, POLL_MS, viewOf, type AssistTimers } from './assist.svelte';
import { autoAvailability, canToggle, limitsText, triggerNote } from './auto';
import {
  actionTitle,
  approveLabel,
  confidencePercent,
  expiryCountdown,
  fallbackReason,
  formatCost,
  lowConfidenceLine,
  riskLabel,
  situationLabel,
  stateLabel,
  stateTone,
  stepDescription,
  stepEffort,
  stepMotion,
  stepOutcomeLabel,
  taskLabel,
} from './labels';
import { visionProblemText } from './problems';
import {
  chooseFamily,
  familyOfModel,
  initialForm,
  keyMissing,
  providerBody,
  validateForm,
} from './provider';
import {
  blobToBase64,
  bytesToBase64,
  cameraImages,
  dataUrlToBase64,
  scaledSize,
  snapshotVideo,
} from './snapshot';

const MOWER = '11111111-1111-4111-8111-111111111111';
const OTHER_MOWER = '22222222-2222-4222-8222-222222222222';

function proposal(overrides: Partial<Proposal> = {}): Proposal {
  return {
    id: 'p1',
    chain_id: 'c1',
    round: 1,
    mower_id: MOWER,
    task: 'recover',
    state: 'analysing',
    final: false,
    requested_by: { kind: 'user', subject: '33333333-3333-4333-8333-333333333333' },
    created_at: '2026-10-10T12:00:00Z',
    updated_at: '2026-10-10T12:00:00Z',
    provider_model: 'fake/recover',
    provider_family: 'fake',
    hosted: false,
    assessment: null,
    steps: [],
    image_count: 0,
    light_used: false,
    stereo: false,
    frames: [],
    trigger: 'manual',
    trigger_error_code: null,
    detail: null,
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
    action: 'drive',
    fallback: false,
    low_confidence: false,
    alternative_action: null,
    model_refused: null,
    approved_option: null,
    options: [],
    can_retry: false,
    rounds_left: 2,
    ...overrides,
  };
}

const STEPS: Proposal['steps'] = [
  { index: 0, linear: -0.4, angular: 0, seconds: 1, outcome: 'pending', frames: 0 },
  { index: 1, linear: 0, angular: 0.5, seconds: 1, outcome: 'pending', frames: 0 },
  { index: 2, linear: 0.4, angular: 0, seconds: 1, outcome: 'pending', frames: 0 },
];

const PROPOSED = proposal({
  state: 'proposed',
  analysed_at: '2026-10-10T12:00:02Z',
  expires_at: '2026-10-10T12:05:02Z',
  assessment: {
    situation: 'stuck_on_obstacle',
    confidence: 0.82,
    action: 'drive',
    rationale: 'A root blocks the front wheels.',
    risk: [],
    abort_reasons: [],
  },
  steps: STEPS,
});

function settings(overrides: Partial<ProviderSettings> = {}): ProviderSettings {
  return {
    configured: true,
    enabled: true,
    model: 'fake/recover',
    family: 'fake',
    api_base: null,
    api_key_set: false,
    header_names: [],
    last_test: null,
    updated_at: '2026-10-10T12:00:00Z',
    hosted_available: false,
    hosted_model: null,
    effective: 'own',
    secrets_available: true,
    ...overrides,
  };
}

function family(overrides: Partial<ProviderFamily> = {}): ProviderFamily {
  return {
    key: 'anthropic',
    label: 'Anthropic',
    prefix: 'anthropic',
    examples: ['anthropic/claude-sonnet-5-5'],
    key_env: 'ANTHROPIC_API_KEY',
    needs_key: true,
    needs_api_base: false,
    default_api_base: null,
    local: false,
    ...overrides,
  };
}

const FAMILIES: ProviderFamily[] = [
  family(),
  family({
    key: 'ollama',
    label: 'Ollama',
    prefix: 'ollama',
    examples: ['ollama/llava'],
    key_env: null,
    needs_key: false,
    needs_api_base: true,
    default_api_base: 'http://localhost:11434',
    local: true,
  }),
  family({
    key: 'openai_compatible',
    label: 'OpenAI compatible',
    prefix: 'openai/compat',
    examples: [],
    key_env: null,
    needs_key: false,
    needs_api_base: true,
    default_api_base: 'http://localhost:8000/v1',
    local: true,
  }),
  family({ key: 'openai', label: 'OpenAI', prefix: 'openai', examples: ['openai/gpt-5'] }),
];

function json(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
    ...init,
  });
}

function problemResponse(status: number, slug: string, detail = 'nope'): Response {
  return json(
    { type: `urn:mowdeck:problem:${slug}`, title: slug, status, detail },
    { status, headers: { 'content-type': 'application/problem+json' } },
  );
}

/** A Vision client whose fetch answers from `handler` (method, path and body of each call). */
function fakeClient(
  handler: (method: string, path: string, body: unknown) => Response | Promise<Response>,
) {
  const calls: { method: string; path: string; body: unknown }[] = [];
  const fetchFn = vi.fn(async (input: Request) => {
    const url = new URL(input.url);
    const text = await input.text();
    const body = text ? (JSON.parse(text) as unknown) : null;
    calls.push({ method: input.method, path: url.pathname + url.search, body });
    return handler(input.method, url.pathname + url.search, body);
  });
  const client = createClient<paths>({
    baseUrl: 'http://vision.test',
    fetch: fetchFn as unknown as typeof fetch,
  });
  return { api: visionCalls(client), calls };
}

/** Timers driven by the test: `tick()` runs the pending poll. */
function manualTimers(): AssistTimers & { tick(): Promise<void>; pending: number } {
  let next: (() => void) | null = null;
  const timers = {
    pending: 0,
    setTimeout: (handler: () => void, ms: number) => {
      expect(ms).toBe(POLL_MS);
      next = handler;
      timers.pending += 1;
      return timers.pending;
    },
    clearTimeout: () => {
      next = null;
    },
    async tick() {
      const run = next;
      next = null;
      run?.();
      await flush();
    },
  };
  return timers;
}

async function flush(): Promise<void> {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
}

const PREFS = {
  stereo_frames: true,
  updated_at: null,
  auto_analysis: false,
  auto_analysis_available: true,
  auto_cooldown_minutes: 10,
  auto_daily_per_mower: 20,
};

function fakeApi(overrides: Partial<VisionApi> = {}): VisionApi {
  const refuse = () => Promise.reject(new Error('not stubbed'));
  return {
    status: refuse,
    providers: refuse,
    provider: vi.fn(async () => settings()),
    saveProvider: refuse,
    deleteProvider: refuse,
    testProvider: refuse,
    preferences: vi.fn(async () => ({
      stereo_frames: true,
      updated_at: null,
      auto_analysis: false,
      auto_analysis_available: true,
      auto_cooldown_minutes: 10,
      auto_daily_per_mower: 20,
    })),
    savePreferences: refuse,
    recover: refuse,
    proposals: vi.fn(async () => []),
    proposal: refuse,
    approve: refuse,
    reject: refuse,
    retry: refuse,
    image: refuse,
    ...overrides,
  };
}

describe('vision step descriptions', () => {
  it('describes single moves in English and German', () => {
    expect(stepDescription(STEPS[0]!, 'en')).toBe('Back 1.0 s');
    expect(stepDescription(STEPS[1]!, 'en')).toBe('Turn left 1.0 s');
    expect(stepDescription(STEPS[2]!, 'en')).toBe('Forward 1.0 s');
    expect(stepDescription({ linear: 0, angular: -0.6, seconds: 1.25 }, 'en')).toBe(
      'Turn right 1.3 s',
    );
    expect(stepDescription(STEPS[0]!, 'de')).toBe('Zurück 1,0 s');
    expect(stepDescription(STEPS[1]!, 'de')).toBe('Links drehen 1,0 s');
  });

  it('describes combined moves and standing still', () => {
    expect(stepDescription({ linear: 0.3, angular: 0.3, seconds: 2 }, 'en')).toBe(
      'Forward and left 2.0 s',
    );
    expect(stepDescription({ linear: 0.3, angular: -0.3, seconds: 2 }, 'en')).toBe(
      'Forward and right 2.0 s',
    );
    expect(stepDescription({ linear: -0.3, angular: 0.3, seconds: 0.5 }, 'en')).toBe(
      'Back and left 0.5 s',
    );
    expect(stepDescription({ linear: -0.3, angular: -0.3, seconds: 0.5 }, 'en')).toBe(
      'Back and right 0.5 s',
    );
    expect(stepDescription({ linear: 0.02, angular: -0.04, seconds: 1 }, 'en')).toBe('Wait 1.0 s');
  });

  it('ignores tiny axis values below the deadband', () => {
    expect(stepMotion({ linear: -0.5, angular: 0.04 })).toBe('back');
    expect(stepMotion({ linear: 0.049, angular: 0.5 })).toBe('left');
    expect(stepEffort({ linear: -0.4, angular: 0.2 })).toBe(40);
    expect(stepEffort({ linear: 3, angular: 0 })).toBe(100);
  });
});

describe('vision labels', () => {
  it('translates situations with a fallback for unknown values', () => {
    expect(situationLabel('wheel_slip', 'en')).toBe('Wheels slipping');
    expect(situationLabel('something_new', 'en')).toBe('Unclear');
    expect(situationLabel('trapped', 'de')).toBe('Eingeklemmt');
  });

  it('has a text and a tone for every state, risk, outcome and task', () => {
    const states = [
      'analysing',
      'proposed',
      'rejected_by_ai',
      'failed',
      'rejected',
      'expired',
      'executing',
      'verifying',
      'recovered',
      'not_recovered',
      'aborted',
    ] as const;
    for (const state of states) {
      expect(stateLabel(state, 'en')).not.toBe('');
      expect(['mowing', 'paused', 'error', 'docked']).toContain(stateTone(state));
    }
    expect(stateTone('recovered')).toBe('mowing');
    expect(stateTone('aborted')).toBe('error');
    for (const flag of ['people_visible', 'animals_visible', 'drop_off', 'water', 'road'] as const)
      expect(riskLabel(flag, 'de')).not.toBe('');
    expect(riskLabel('road', 'en')).toBe('Road nearby');
    for (const outcome of ['pending', 'done', 'aborted', 'skipped'] as const)
      expect(stepOutcomeLabel(outcome, 'en')).not.toBe('');
    expect(taskLabel('find_dock', 'en')).toBe('Find dock');
    expect(taskLabel('recover', 'de')).toBe('Befreien');
  });

  it('formats confidence, the expiry countdown and the cost estimate', () => {
    expect(confidencePercent(0.824)).toBe(82);
    expect(confidencePercent(1.4)).toBe(100);
    expect(confidencePercent(-1)).toBe(0);
    const now = Date.parse('2026-10-10T12:00:00Z');
    expect(expiryCountdown('2026-10-10T12:04:05Z', now)).toBe('4:05');
    expect(expiryCountdown('2026-10-10T11:59:00Z', now)).toBe('0:00');
    expect(expiryCountdown(null, now)).toBeNull();
    expect(expiryCountdown('soon', now)).toBeNull();
    expect(formatCost(0.0042, 'en')).toBe('$0.0042');
    expect(formatCost(1.5, 'en')).toBe('$1.50');
    expect(formatCost(null, 'en')).toBeNull();
    expect(formatCost(Number.NaN, 'en')).toBeNull();
  });

  it('names the vision problems', () => {
    const missing = new ApiProblem({
      type: 'urn:mowdeck:problem:vision-provider-missing',
      status: 409,
    });
    expect(visionProblemText(missing).title).toBe(m.vision_problem_provider_missing());
    expect(isVisionProblem(missing, 'vision-provider-missing')).toBe(true);
    expect(isVisionProblem(null, 'vision-provider-missing')).toBe(false);
    const quota = new ApiProblem({
      type: 'urn:mowdeck:problem:vision-quota',
      status: 429,
      detail: '20 per day',
    });
    expect(visionProblemText(quota)).toEqual({
      title: m.vision_problem_quota(),
      detail: '20 per day',
    });
    expect(
      visionProblemText(
        new ApiProblem({ type: 'urn:mowdeck:problem:vision-proposal-expired', status: 409 }),
      ).title,
    ).toBe(m.vision_problem_expired());
    expect(
      visionProblemText(
        new ApiProblem({ type: 'urn:mowdeck:problem:vision-secrets-unavailable', status: 409 }),
      ).title,
    ).toBe(m.vision_problem_secrets());
    expect(
      visionProblemText(
        new ApiProblem({ type: 'urn:mowdeck:problem:vision-provider-failed', status: 502 }),
      ).title,
    ).toBe(m.vision_problem_provider_failed());
    expect(visionProblemText(ApiProblem.network()).title).toBe(m.problem_network());
  });
});

describe('vision snapshot', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    document.body.innerHTML = '';
  });

  it('encodes bytes, data URLs and blobs as standard base64', async () => {
    const bytes = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10]);
    expect(bytesToBase64(bytes)).toBe('/9j/4AAQ');
    expect(bytesToBase64(new Uint8Array())).toBe('');
    expect(dataUrlToBase64('data:image/jpeg;base64,/9j/4AAQ')).toBe('/9j/4AAQ');
    expect(dataUrlToBase64('data:,')).toBeNull();
    expect(await blobToBase64(new Blob([bytes], { type: 'image/jpeg' }))).toBe('/9j/4AAQ');
    // Larger than one chunk: the chunked conversion matches a plain one.
    const big = new Uint8Array(70_000).map((_, i) => i % 256);
    expect(atob(bytesToBase64(big)).length).toBe(70_000);
  });

  it('scales frames down to the maximum width, never up', () => {
    expect(scaledSize(2560, 1440)).toEqual({ width: 1280, height: 720 });
    expect(scaledSize(640, 480)).toEqual({ width: 640, height: 480 });
    expect(scaledSize(4000, 1, 1000)).toEqual({ width: 1000, height: 1 });
  });

  it('sends no image without a camera video or before the first frame', async () => {
    expect(await cameraImages()).toEqual([]);
    const video = document.createElement('video');
    video.setAttribute('data-camera-video', '');
    document.body.append(video);
    expect(await snapshotVideo(video)).toBeNull();
    expect(await cameraImages()).toEqual([]);
  });

  it('draws the camera frame into a scaled canvas and returns a JPEG', async () => {
    const draw = vi.fn();
    const sizes: number[][] = [];
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(function (
      this: HTMLCanvasElement,
    ) {
      sizes.push([this.width, this.height]);
      return { drawImage: draw } as unknown as CanvasRenderingContext2D;
    } as never);
    vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(
      (callback: BlobCallback, type?: string, quality?: unknown) => {
        expect(type).toBe('image/jpeg');
        expect(quality).toBe(0.8);
        callback(new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10])]));
      },
    );
    const video = document.createElement('video');
    video.setAttribute('data-camera-video', '');
    Object.defineProperty(video, 'videoWidth', { value: 1920 });
    Object.defineProperty(video, 'videoHeight', { value: 1080 });
    document.body.append(video);
    expect(await cameraImages()).toEqual([{ media_type: 'image/jpeg', data: '/9j/4AAQ' }]);
    expect(sizes).toEqual([[1280, 720]]);
    expect(draw).toHaveBeenCalledWith(video, 0, 0, 1280, 720);
  });

  it('falls back to a data URL when toBlob gives nothing', async () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      drawImage: vi.fn(),
    } as never);
    vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation((callback) =>
      callback(null),
    );
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue(
      'data:image/jpeg;base64,QUJD',
    );
    const video = document.createElement('video');
    Object.defineProperty(video, 'videoWidth', { value: 320 });
    Object.defineProperty(video, 'videoHeight', { value: 240 });
    expect(await snapshotVideo(video)).toEqual({ media_type: 'image/jpeg', data: 'QUJD' });
  });
});

describe('vision api calls', () => {
  it('posts a recover request with the task and the images', async () => {
    const { api, calls } = fakeClient(() => json(proposal(), { status: 202 }));
    const created = await api.recover(MOWER, 'find_dock', [
      { media_type: 'image/jpeg', data: 'QUJD' },
    ]);
    expect(created.id).toBe('p1');
    expect(calls).toEqual([
      {
        method: 'POST',
        path: `/api/v1/vision/mowers/${MOWER}/recover`,
        body: { task: 'find_dock', images: [{ media_type: 'image/jpeg', data: 'QUJD' }] },
      },
    ]);
  });

  it('lists proposals newest first and reads, approves, rejects and retries one', async () => {
    const { api, calls } = fakeClient((method, path) => {
      if (path.includes('/proposals?')) return json({ items: [PROPOSED], next_before: null });
      if (path.endsWith('/retry')) return json(proposal({ id: 'p2', round: 2 }), { status: 202 });
      return json(PROPOSED);
    });
    expect(await api.proposals(MOWER, 1)).toEqual([PROPOSED]);
    expect((await api.proposal('p1')).state).toBe('proposed');
    await api.approve('p1');
    await api.reject('p1');
    expect((await api.retry('p1', [])).round).toBe(2);
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual([
      `GET /api/v1/vision/mowers/${MOWER}/proposals?limit=1`,
      'GET /api/v1/vision/proposals/p1',
      'POST /api/v1/vision/proposals/p1/approve',
      'POST /api/v1/vision/proposals/p1/reject',
      'POST /api/v1/vision/proposals/p1/retry',
    ]);
    expect(calls[4]!.body).toEqual({ images: [] });
  });

  it('saves the provider without echoing a key and turns refusals into problems', async () => {
    const { api, calls } = fakeClient((method) => {
      if (method === 'PUT')
        return problemResponse(409, 'vision-secrets-unavailable', 'no secrets key');
      if (method === 'DELETE') return new Response(null, { status: 204 });
      return json(settings());
    });
    const refused = await api
      .saveProvider({ model: 'anthropic/claude-sonnet-5-5', enabled: true, api_key: 'sk-1' })
      .catch((error: unknown) => error);
    expect(refused).toBeInstanceOf(ApiProblem);
    expect((refused as ApiProblem).slug).toBe('vision-secrets-unavailable');
    await api.deleteProvider();
    expect((await api.provider()).effective).toBe('own');
    expect(calls.map((c) => c.method)).toEqual(['PUT', 'DELETE', 'GET']);
  });

  it('reads the status, the families, a test and an image', async () => {
    const { api } = fakeClient((method, path) => {
      if (path.endsWith('/status'))
        return json({
          hosted_default_enabled: false,
          hosted_default_model: null,
          secrets_available: true,
          allow_fake_provider: true,
          user_api_base_policy: 'any',
        });
      if (path.endsWith('/providers'))
        return json({
          families: FAMILIES,
          hosted_available: false,
          hosted_model: null,
          secrets_available: true,
        });
      if (path.endsWith('/test'))
        return json({
          ok: true,
          model: 'fake/recover',
          family: 'fake',
          latency_ms: 12,
          input_tokens: 10,
          output_tokens: 5,
          cost_usd: 0,
          detail: null,
        });
      return new Response(new Uint8Array([1, 2, 3]), {
        headers: { 'content-type': 'image/jpeg' },
      });
    });
    expect((await api.status()).allow_fake_provider).toBe(true);
    expect((await api.providers()).families).toHaveLength(4);
    expect((await api.testProvider()).ok).toBe(true);
    expect((await api.image('p1', 0)).size).toBe(3);
  });
});

describe('vision automatic analysis', () => {
  const AUTO = {
    stereo_frames: true,
    updated_at: null,
    auto_analysis: false,
    auto_analysis_available: true,
    auto_cooldown_minutes: 10,
    auto_daily_per_mower: 20,
  };

  it('reads and saves the opt-in', async () => {
    const { api, calls } = fakeClient((method) =>
      json({ ...AUTO, auto_analysis: method === 'PUT' }),
    );
    expect((await api.preferences()).auto_analysis).toBe(false);
    expect((await api.savePreferences({ auto_analysis: true })).auto_analysis).toBe(true);
    expect(calls).toEqual([
      { method: 'GET', path: '/api/v1/vision/me/preferences', body: null },
      { method: 'PUT', path: '/api/v1/vision/me/preferences', body: { auto_analysis: true } },
    ]);
  });

  it('turns a missing plan feature into a problem', async () => {
    const { api } = fakeClient(() => problemResponse(403, 'feature-not-in-plan', 'no ai_recover'));
    const refused = await api
      .savePreferences({ auto_analysis: true })
      .catch((error: unknown) => error);
    expect(refused).toBeInstanceOf(ApiProblem);
    expect(isVisionProblem(refused as ApiProblem, 'feature-not-in-plan')).toBe(true);
  });

  it('can only be switched on with a provider on an installation that allows it', () => {
    expect(autoAvailability(AUTO, settings())).toBe('ready');
    expect(autoAvailability(AUTO, settings({ effective: 'none' }))).toBe('no-provider');
    expect(autoAvailability(AUTO, null)).toBe('no-provider');
    expect(autoAvailability({ ...AUTO, auto_analysis_available: false }, settings())).toBe(
      'unavailable',
    );
    expect(canToggle(AUTO, 'ready')).toBe(true);
    expect(canToggle(AUTO, 'no-provider')).toBe(false);
    expect(canToggle({ ...AUTO, auto_analysis: true }, 'unavailable')).toBe(true);
  });

  it('shows the limits and the error that started a proposal', () => {
    expect(limitsText(AUTO)).toBe(m.ai_auto_limits({ minutes: 10, daily: 20 }));
    expect(triggerNote(proposal())).toBeNull();
    expect(triggerNote(proposal({ trigger: 'error', trigger_error_code: '1201' }))).toBe(
      m.vision_trigger_error({ code: '1201' }),
    );
    expect(triggerNote(proposal({ trigger: 'error', trigger_error_code: null }))).toBe(
      m.vision_trigger_error({ code: '?' }),
    );
  });
});

describe('vision assist store', () => {
  it('starts ready when the mower has no open proposal', async () => {
    const api = fakeApi();
    const store = new AssistStore(api, manualTimers());
    expect(store.view).toBe('ready');
    await store.open(MOWER);
    expect(store.view).toBe('ready');
    expect(store.provider?.effective).toBe('own');
    expect(api.proposals).toHaveBeenCalledWith(MOWER, 1);
  });

  it('shows the open proposal of the mower instead of starting a new one', async () => {
    const timers = manualTimers();
    const store = new AssistStore(fakeApi({ proposals: async () => [PROPOSED] }), timers);
    await store.open(MOWER);
    expect(store.view).toBe('proposed');
    expect(store.proposal?.id).toBe('p1');
    expect(timers.pending).toBe(1);
  });

  it('ignores a final newest proposal and a linked proposal of another mower', async () => {
    const api = fakeApi({
      proposals: vi.fn(async () => [proposal({ state: 'recovered', final: true })]),
      proposal: vi.fn(async () => proposal({ id: 'px', mower_id: OTHER_MOWER })),
    });
    const store = new AssistStore(api, manualTimers());
    await store.open(MOWER, 'px');
    expect(store.view).toBe('ready');
    expect(api.proposal).toHaveBeenCalledWith('px');
  });

  it('runs a request through analysis, approval and execution to the outcome', async () => {
    const timers = manualTimers();
    const sequence = [
      PROPOSED,
      { ...PROPOSED, state: 'executing' as const },
      {
        ...PROPOSED,
        state: 'recovered' as const,
        final: true,
        steps: STEPS.map((s) => ({ ...s, outcome: 'done' as const })),
      },
    ];
    const api = fakeApi({
      recover: vi.fn(async () => proposal()),
      proposal: vi.fn(async () => sequence.shift()!),
      approve: vi.fn(async () => ({ ...PROPOSED, state: 'executing' as const })),
    });
    const store = new AssistStore(api, timers);
    await store.open(MOWER);
    await store.start('recover', [{ media_type: 'image/jpeg', data: 'QUJD' }]);
    expect(api.recover).toHaveBeenCalledWith(MOWER, 'recover', [
      { media_type: 'image/jpeg', data: 'QUJD' },
    ]);
    expect(store.view).toBe('analysing');
    await timers.tick();
    expect(store.view).toBe('proposed');
    await store.approve();
    expect(api.approve).toHaveBeenCalledWith('p1');
    expect(store.view).toBe('executing');
    await timers.tick();
    expect(store.view).toBe('executing');
    await timers.tick();
    expect(store.view).toBe('outcome');
    expect(store.proposal?.state).toBe('recovered');
    expect(store.canRetry).toBe(false);
    // Final: no further poll is scheduled.
    const before = timers.pending;
    await timers.tick();
    expect(timers.pending).toBe(before);
  });

  it('loads the open proposal when a start conflicts with it', async () => {
    const proposals = vi
      .fn<VisionApi['proposals']>()
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([PROPOSED]);
    const store = new AssistStore(
      fakeApi({
        proposals,
        recover: async () => {
          throw new ApiProblem({ type: 'urn:mowdeck:problem:conflict', status: 409 });
        },
      }),
      manualTimers(),
    );
    await store.open(MOWER);
    await store.start('recover');
    expect(store.problem).toBeNull();
    expect(store.view).toBe('proposed');
  });

  it('reports a missing provider with a link to the settings', async () => {
    const store = new AssistStore(
      fakeApi({
        provider: async () => settings({ configured: false, effective: 'none', model: null }),
        recover: async () => {
          throw new ApiProblem({
            type: 'urn:mowdeck:problem:vision-provider-missing',
            status: 409,
          });
        },
      }),
      manualTimers(),
    );
    await store.open(MOWER);
    expect(store.providerMissing).toBe(true);
    await store.start('find_dock');
    expect(store.problem?.slug).toBe('vision-provider-missing');
    expect(store.view).toBe('ready');
  });

  it('keeps a quota refusal as the problem', async () => {
    const store = new AssistStore(
      fakeApi({
        recover: async () => {
          throw new ApiProblem({ type: 'urn:mowdeck:problem:vision-quota', status: 429 });
        },
      }),
      manualTimers(),
    );
    await store.open(MOWER);
    await store.start('recover');
    expect(store.problem?.status).toBe(429);
    expect(store.providerMissing).toBe(false);
  });

  it('rejects a proposal and shows the new state after a refused approval', async () => {
    const api = fakeApi({
      proposals: async () => [PROPOSED],
      approve: async () => {
        throw new ApiProblem({
          type: 'urn:mowdeck:problem:vision-proposal-expired',
          status: 409,
        });
      },
      proposal: vi.fn(async () => ({ ...PROPOSED, state: 'expired' as const, final: true })),
      reject: vi.fn(async () => ({ ...PROPOSED, state: 'rejected' as const, final: true })),
    });
    const store = new AssistStore(api, manualTimers());
    await store.open(MOWER);
    await store.approve();
    expect(store.problem?.slug).toBe('vision-proposal-expired');
    expect(store.proposal?.state).toBe('expired');
    expect(store.view).toBe('outcome');

    const second = new AssistStore(
      fakeApi({ proposals: async () => [PROPOSED], reject: api.reject }),
      manualTimers(),
    );
    await second.open(MOWER);
    await second.reject();
    expect(second.proposal?.state).toBe('rejected');
  });

  it('retries a failed round when the service allows it', async () => {
    const failed = {
      ...PROPOSED,
      state: 'not_recovered' as const,
      final: true,
      can_retry: true,
      rounds_left: 1,
    };
    const api = fakeApi({
      proposal: async () => failed,
      retry: vi.fn(async () => proposal({ id: 'p2', round: 2 })),
    });
    const store = new AssistStore(api, manualTimers());
    await store.open(MOWER, 'p1');
    expect(store.canRetry).toBe(true);
    await store.retry([]);
    expect(api.retry).toHaveBeenCalledWith('p1', []);
    expect(store.proposal?.id).toBe('p2');
    expect(store.view).toBe('analysing');
  });

  it('stops polling on close and when the proposal is gone', async () => {
    const timers = manualTimers();
    const proposalCall = vi.fn(async () => {
      throw new ApiProblem({ status: 404 });
    });
    const store = new AssistStore(
      fakeApi({ proposals: async () => [PROPOSED], proposal: proposalCall }),
      timers,
    );
    await store.open(MOWER);
    await timers.tick();
    expect(store.problem?.status).toBe(404);
    const after = timers.pending;
    await timers.tick();
    expect(timers.pending).toBe(after);

    await store.open(MOWER);
    store.close();
    expect(store.proposal).toBeNull();
    await timers.tick();
    expect(proposalCall).toHaveBeenCalledTimes(1);
  });

  it('keeps polling through a network failure', async () => {
    const timers = manualTimers();
    const proposalCall = vi
      .fn<VisionApi['proposal']>()
      .mockRejectedValueOnce(ApiProblem.network())
      .mockResolvedValueOnce({ ...PROPOSED, state: 'executing' });
    const store = new AssistStore(
      fakeApi({ proposals: async () => [PROPOSED], proposal: proposalCall }),
      timers,
    );
    await store.open(MOWER);
    await timers.tick();
    expect(store.view).toBe('proposed');
    await timers.tick();
    expect(store.view).toBe('executing');
  });

  it('maps proposal states to sheet views', () => {
    expect(viewOf(null)).toBe('ready');
    expect(viewOf(proposal({ state: 'verifying' }))).toBe('executing');
    expect(viewOf(proposal({ state: 'failed', final: true }))).toBe('outcome');
    expect(viewOf(proposal({ state: 'rejected_by_ai' }))).toBe('outcome');
  });

  it('records a provider that cannot be loaded without blocking the sheet', async () => {
    const store = new AssistStore(
      fakeApi({
        provider: async () => {
          throw ApiProblem.network();
        },
      }),
      manualTimers(),
    );
    await store.open(MOWER);
    expect(store.provider).toBeNull();
    expect(store.providerProblem?.slug).toBe('network');
    expect(store.view).toBe('ready');
  });
});

describe('vision provider form', () => {
  it('finds the family of a model string by the longest prefix', () => {
    expect(familyOfModel('anthropic/claude-sonnet-5-5', FAMILIES)?.key).toBe('anthropic');
    expect(familyOfModel('openai/compat/llava', FAMILIES)?.key).toBe('openai_compatible');
    expect(familyOfModel('openai/gpt-5', FAMILIES)?.key).toBe('openai');
    expect(familyOfModel('unknown/x', FAMILIES)).toBeNull();
  });

  it('pre-fills the first family or the stored settings', () => {
    const fresh = initialForm(null, FAMILIES);
    expect(fresh).toMatchObject({ family: 'anthropic', model: 'anthropic/claude-sonnet-5-5' });
    const stored = initialForm(
      settings({ model: 'ollama/llava', family: 'ollama', api_base: 'http://gpu:11434' }),
      FAMILIES,
    );
    expect(stored).toMatchObject({
      family: 'ollama',
      apiBase: 'http://gpu:11434',
      apiKey: '',
      enabled: true,
    });
    expect(initialForm(settings({ family: null, model: 'openai/gpt-5' }), FAMILIES).family).toBe(
      'openai',
    );
    expect(initialForm(null, []).family).toBe('');
  });

  it('switches families with their example and base URL', () => {
    const form = chooseFamily(initialForm(null, FAMILIES), FAMILIES[1]!);
    expect(form).toMatchObject({
      family: 'ollama',
      model: 'ollama/llava',
      apiBase: 'http://localhost:11434',
    });
    const compatible = chooseFamily(form, FAMILIES[2]!);
    expect(compatible.model).toBe('openai/compat/');
    expect(chooseFamily(compatible, FAMILIES[0]!).apiBase).toBe('');
  });

  it('validates the model and the base URL', () => {
    const ollama = FAMILIES[1]!;
    const base = chooseFamily(initialForm(null, FAMILIES), ollama);
    expect(validateForm(base, ollama)).toEqual({});
    expect(validateForm({ ...base, model: '' }, ollama).model).toBe(m.ai_provider_error_model());
    expect(validateForm({ ...base, model: 'anthropic/x' }, ollama).model).toBe(
      m.ai_provider_error_prefix({ prefix: 'ollama/' }),
    );
    expect(validateForm({ ...base, apiBase: '' }, ollama).apiBase).toBe(m.ai_provider_error_base());
    expect(validateForm({ ...base, apiBase: 'ftp://x' }, ollama).apiBase).toBe(
      m.ai_provider_error_base_url(),
    );
  });

  it('sends the key only when typed in, or empty to remove it', () => {
    const anthropic = FAMILIES[0]!;
    const form = initialForm(settings({ model: 'anthropic/x', family: 'anthropic' }), FAMILIES);
    expect(providerBody(form, anthropic)).toEqual({
      model: 'anthropic/x',
      enabled: true,
      api_base: null,
    });
    expect(providerBody({ ...form, apiKey: ' sk-1 ' }, anthropic).api_key).toBe('sk-1');
    expect(providerBody({ ...form, removeKey: true }, anthropic).api_key).toBe('');
    const ollama = chooseFamily(form, FAMILIES[1]!);
    expect(providerBody(ollama, FAMILIES[1]!).api_base).toBe('http://localhost:11434');
  });

  it('hints at a missing key only when none is typed in or stored', () => {
    const anthropic = FAMILIES[0]!;
    const form = initialForm(null, FAMILIES);
    expect(keyMissing(form, anthropic, settings({ api_key_set: false }))).toBe(true);
    expect(keyMissing(form, anthropic, settings({ api_key_set: true }))).toBe(false);
    expect(
      keyMissing({ ...form, removeKey: true }, anthropic, settings({ api_key_set: true })),
    ).toBe(true);
    expect(keyMissing({ ...form, apiKey: 'sk' }, anthropic, null)).toBe(false);
    expect(keyMissing(form, FAMILIES[1]!, null)).toBe(false);
  });
});

describe('assist store: stereo preference', () => {
  it('loads the stereo preference and keeps the default when it cannot be read', async () => {
    const off = new AssistStore(
      fakeApi({ preferences: async () => ({ ...PREFS, stereo_frames: false }) }),
      manualTimers(),
    );
    await off.open(MOWER);
    await flush();
    expect(off.stereo).toBe(false);
    const broken = new AssistStore(
      fakeApi({ preferences: () => Promise.reject(new Error('offline')) }),
      manualTimers(),
    );
    await broken.open(MOWER);
    await flush();
    expect(broken.stereo).toBe(true);
  });

  it('saves the preference through the API', async () => {
    const { api, calls } = fakeClient(() => json({ stereo_frames: false, updated_at: null }));
    expect((await api.savePreferences({ stereo_frames: false })).stereo_frames).toBe(false);
    expect(await api.preferences()).toEqual({ stereo_frames: false, updated_at: null });
    expect(calls.map((c) => [c.method, c.path])).toEqual([
      ['PUT', '/api/v1/vision/me/preferences'],
      ['GET', '/api/v1/vision/me/preferences'],
    ]);
    expect(calls[0]?.body).toEqual({ stereo_frames: false });
  });
});

describe('vision low-confidence options', () => {
  const CHOICE = proposal({
    ...PROPOSED,
    action: 'return_to_dock',
    fallback: true,
    low_confidence: true,
    alternative_action: 'resume',
    assessment: { ...PROPOSED.assessment!, confidence: 0.55, action: 'resume' },
    options: [
      {
        option: 'fallback',
        action: 'return_to_dock',
        default: true,
        low_confidence: false,
        confidence: null,
        steps: [],
      },
      {
        option: 'model',
        action: 'resume',
        default: false,
        low_confidence: true,
        confidence: 0.55,
        steps: STEPS,
      },
    ],
  });

  it('says what the model recommends and how sure it is, in both languages', () => {
    expect(lowConfidenceLine('resume', 0.55, 'de')).toBe(
      'Die KI empfiehlt „Mähauftrag fortsetzen“, aber nur mit 55 % Sicherheit.',
    );
    expect(lowConfidenceLine('resume', 0.55, 'en')).toBe(
      'The AI recommends “Continue the mowing job”, but only with 55 % confidence.',
    );
    expect(actionTitle('drive', 'de')).toBe('Freifahren (Messer aus)');
    expect(approveLabel('resume', 'en')).toBe(m.vision_approve_resume({}, { locale: 'en' }));
    expect(approveLabel('return_to_dock', 'en')).toBe(m.vision_approve_dock({}, { locale: 'en' }));
  });

  it('claims "no recommendation" only when the model recommended nothing', () => {
    expect(fallbackReason(CHOICE)).toBe('choice');
    const refused = proposal({ ...CHOICE, alternative_action: null, model_refused: 'geofence' });
    expect(fallbackReason(refused)).toBe('refused');
    const none = proposal({
      ...CHOICE,
      alternative_action: null,
      assessment: { ...CHOICE.assessment!, action: 'none' },
    });
    expect(fallbackReason(none)).toBe('none');
  });

  it('sends the chosen option with the approval, nothing without a choice', async () => {
    const { api, calls } = fakeClient(() => json(CHOICE));
    await api.approve('p1', 'model');
    await api.approve('p1');
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual([
      'POST /api/v1/vision/proposals/p1/approve',
      'POST /api/v1/vision/proposals/p1/approve',
    ]);
    expect(calls[0]!.body).toEqual({ option: 'model' });
    expect(calls[1]!.body).toBeNull();
  });

  it('approves the picked option through the store', async () => {
    const timers = manualTimers();
    const api = fakeApi({
      proposals: vi.fn(async () => [CHOICE]),
      proposal: vi.fn(async () => CHOICE),
      approve: vi.fn(async () => ({ ...CHOICE, state: 'executing' as const })),
    });
    const store = new AssistStore(api, timers);
    await store.open(MOWER);
    expect(store.view).toBe('proposed');
    await store.approve('model');
    expect(api.approve).toHaveBeenCalledWith('p1', 'model');
  });
});

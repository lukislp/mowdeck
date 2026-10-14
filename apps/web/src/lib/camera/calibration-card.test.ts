/**
 * The panorama's calibration card: the method chooser, the run with its result (confidence,
 * inliers, before/after preview), apply and discard, the AI method's cost note and the link to
 * the provider settings when none is set up.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { m } from '$lib/paraglide/messages';
import { PROVIDER_SETTINGS_ROUTE } from '$lib/vision/api';

import CalibrationCard from './CalibrationCard.svelte';
import { manualHomography } from './calibration/manual';
import type { CalibrationMethods } from './calibration/methods';
import { defaultCalibration } from './calibration/storage';
import {
  CalibrationError,
  type CalibrationMethod,
  type CalibrationResult,
  type PanoramaCalibration,
} from './calibration/types';

vi.mock('./calibration/capture', () => {
  const frame = {
    width: 64,
    height: 48,
    gray: { width: 64, height: 48, data: new Float32Array(64 * 48) },
    capturedAt: 0,
    jpeg: async () => '',
  };
  return {
    capturePair: () => ({
      left: frame,
      right: frame,
      leftCanvas: document.createElement('canvas'),
      rightCanvas: document.createElement('canvas'),
      at: new Date('2026-10-13T10:00:00Z'),
    }),
  };
});

const RESULT: CalibrationResult = {
  method: 'features',
  homography: manualHomography({ overlap: 0.2, offset: 0.01, mirror: false }),
  confidence: 0.82,
  inliers: 41,
  matches: 57,
};

function methods(estimate: CalibrationMethod['estimate']): CalibrationMethods {
  const method = (id: CalibrationMethod['id']): CalibrationMethod => ({
    id,
    label: () => id,
    estimate,
  });
  return {
    manual: method('manual'),
    correlation: method('correlation'),
    features: method('features'),
    ai: method('ai'),
  };
}

function setup(
  props: {
    estimate?: CalibrationMethod['estimate'];
    provider?: () => Promise<'own' | 'hosted' | 'none'>;
    calibration?: PanoramaCalibration;
    live?: boolean;
  } = {},
) {
  const onchange = vi.fn();
  const estimate = props.estimate ?? vi.fn(async () => RESULT);
  render(CalibrationCard, {
    props: {
      mowerId: 'm1',
      calibration: props.calibration ?? defaultCalibration(),
      onchange,
      sources: () => [null, null] as const,
      live: props.live ?? true,
      methods: methods(estimate),
      provider: props.provider ?? (async () => 'own' as const),
    },
  });
  return { onchange, estimate };
}

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('calibration card', () => {
  it('offers the three methods and shows the sliders only for the manual one', async () => {
    setup();
    const chooser = screen.getByTestId('camera-calibration-method');
    expect(chooser.getAttribute('role')).toBe('radiogroup');
    for (const id of ['manual', 'features', 'ai']) {
      expect(screen.getByTestId(`camera-calibration-method-${id}`)).toBeInTheDocument();
    }
    expect(screen.getByTestId('camera-calibration-method-manual').textContent).toContain(
      m.camera_calibration_method_manual(),
    );
    expect(
      screen.getByTestId('camera-calibration-method-manual').getAttribute('aria-checked'),
    ).toBe('true');
    expect(screen.getByTestId('camera-panorama-overlap')).toBeInTheDocument();
    expect(screen.queryByTestId('camera-calibration-run')).toBeNull();
    await fireEvent.click(screen.getByTestId('camera-calibration-method-features'));
    expect(
      screen.getByTestId('camera-calibration-method-features').getAttribute('aria-checked'),
    ).toBe('true');
    expect(screen.queryByTestId('camera-panorama-overlap')).toBeNull();
    expect(screen.getByTestId('camera-calibration-scene').textContent).toBe(
      m.camera_calibration_scene(),
    );
    expect(screen.getByTestId('camera-calibration-run')).toBeInTheDocument();
  });

  it('saves slider changes at once as a manual calibration', async () => {
    const { onchange } = setup();
    await fireEvent.input(screen.getByTestId('camera-panorama-overlap'), {
      target: { value: '25' },
    });
    expect(onchange).toHaveBeenLastCalledWith(
      expect.objectContaining({ method: 'manual', overlapPercent: 25 }),
    );
  });

  it('runs the local method, shows the result with a preview and applies it', async () => {
    const { onchange, estimate } = setup();
    await fireEvent.click(screen.getByTestId('camera-calibration-method-features'));
    await fireEvent.click(screen.getByTestId('camera-calibration-run'));
    const result = await screen.findByTestId('camera-calibration-result');
    expect(estimate).toHaveBeenCalledTimes(1);
    expect(result.dataset.method).toBe('features');
    expect(result.dataset.confidence).toBe('82');
    expect(result.dataset.inliers).toBe('41');
    expect(result.textContent).toContain(m.camera_calibration_confidence({ percent: '82' }));
    expect(screen.getByTestId('camera-calibration-before')).toBeInTheDocument();
    expect(screen.getByTestId('camera-calibration-after')).toBeInTheDocument();
    expect(onchange).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByTestId('camera-calibration-apply'));
    expect(onchange).toHaveBeenCalledWith(
      expect.objectContaining({
        method: 'features',
        inliers: 41,
        capturedAt: '2026-10-13T10:00:00.000Z',
      }),
    );
    expect(screen.queryByTestId('camera-calibration-result')).toBeNull();
  });

  it('discards a result without storing anything and explains failures', async () => {
    let fail = false;
    const { onchange } = setup({
      estimate: async () => {
        if (fail) throw new CalibrationError('no_match');
        return RESULT;
      },
    });
    await fireEvent.click(screen.getByTestId('camera-calibration-method-features'));
    await fireEvent.click(screen.getByTestId('camera-calibration-run'));
    await screen.findByTestId('camera-calibration-result');
    await fireEvent.click(screen.getByTestId('camera-calibration-discard'));
    expect(screen.queryByTestId('camera-calibration-result')).toBeNull();
    expect(onchange).not.toHaveBeenCalled();
    fail = true;
    await fireEvent.click(screen.getByTestId('camera-calibration-run'));
    const error = await screen.findByTestId('camera-calibration-error');
    expect(error.dataset.reason).toBe('no_match');
    expect(error.textContent).toContain(m.camera_calibration_error_no_match());
  });

  it('needs both live pictures to calibrate', async () => {
    setup({ live: false });
    await fireEvent.click(screen.getByTestId('camera-calibration-method-features'));
    expect(screen.getByTestId('camera-calibration-run')).toBeDisabled();
    expect(screen.getByTestId('camera-calibration-needs-live')).toBeInTheDocument();
  });

  it('shows the AI cost note and links to the provider settings without a provider', async () => {
    setup({ provider: async () => 'none' });
    await fireEvent.click(screen.getByTestId('camera-calibration-method-ai'));
    expect(screen.getByTestId('camera-calibration-ai-cost').textContent).toBe(
      m.camera_calibration_ai_cost(),
    );
    const missing = await screen.findByTestId('camera-calibration-ai-missing');
    expect(missing.querySelector('a')?.getAttribute('href')).toBe(PROVIDER_SETTINGS_ROUTE);
    await waitFor(() => expect(screen.getByTestId('camera-calibration-run')).toBeDisabled());
    expect(screen.queryByTestId('camera-panorama-mirror')).toBeNull();
  });

  it('starts on the stored method and resets to the default sliders', async () => {
    const stored: PanoramaCalibration = {
      method: 'ai',
      homography: RESULT.homography,
      mirror: false,
      capturedAt: '2026-10-12T08:00:00Z',
      confidence: 0.7,
      inliers: 9,
    };
    const { onchange } = setup({ calibration: stored });
    expect(screen.getByTestId('camera-calibration-method-ai').getAttribute('aria-checked')).toBe(
      'true',
    );
    expect(screen.getByTestId('camera-calibration-active').dataset.method).toBe('ai');
    await fireEvent.click(screen.getByTestId('camera-panorama-reset'));
    expect(onchange).toHaveBeenCalledWith(defaultCalibration());
  });
});

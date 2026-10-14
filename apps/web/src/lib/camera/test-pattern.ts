/**
 * The generated test pattern of a fake camera session (the simulator, ADR 0032): colour bars,
 * a moving sweep, the time and the mower's position, so the camera path is visible end to end
 * without any video service. Pure drawing on a 2D context; the colours come from the design
 * tokens (read by the caller). The right camera of a stereo pair is drawn mirrored (bars and
 * sweep run the other way) and every frame names its camera, so two views are told apart at a
 * glance and in a captured frame.
 */

export interface PatternColors {
  /** Bars, left to right. */
  bars: string[];
  background: string;
  ink: string;
  inkInverse: string;
  sweep: string;
}

export interface PatternFrame {
  /** Time stamp shown in the picture (milliseconds since the epoch). */
  now: number;
  /** Line under the clock, e.g. the mower and camera name. */
  label: string;
  /** Position text (latitude, longitude) or null without a fix. */
  position: string | null;
  /** Frame counter; moves the sweep. */
  tick: number;
  /** Draw bars and sweep mirrored (the right camera of a stereo pair). */
  mirror?: boolean;
}

/** The subset of `CanvasRenderingContext2D` the pattern uses (a fake in tests). */
export type PatternContext = Pick<
  CanvasRenderingContext2D,
  'fillRect' | 'fillText' | 'strokeRect' | 'beginPath' | 'arc' | 'stroke' | 'moveTo' | 'lineTo'
> & {
  fillStyle: CanvasRenderingContext2D['fillStyle'];
  strokeStyle: CanvasRenderingContext2D['strokeStyle'];
  lineWidth: number;
  font: string;
  textAlign: CanvasTextAlign;
  textBaseline: CanvasTextBaseline;
};

const pad = (value: number) => String(value).padStart(2, '0');

/** `HH:MM:SS` in local time. */
export function clockText(now: number): string {
  const at = new Date(now);
  return `${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`;
}

/** `48.13715, 11.57612` or null. */
export function positionText(position: { lat: number; lon: number } | null | undefined) {
  if (!position || !Number.isFinite(position.lat) || !Number.isFinite(position.lon)) return null;
  return `${position.lat.toFixed(5)}, ${position.lon.toFixed(5)}`;
}

/** Draw one frame of the pattern onto `ctx` (width x height pixels). */
export function drawTestPattern(
  ctx: PatternContext,
  width: number,
  height: number,
  frame: PatternFrame,
  colors: PatternColors,
): void {
  ctx.fillStyle = colors.background;
  ctx.fillRect(0, 0, width, height);
  const palette = colors.bars.length ? colors.bars : [colors.ink];
  const bars = frame.mirror ? [...palette].reverse() : palette;
  const barHeight = Math.round(height * 0.62);
  const barWidth = width / bars.length;
  bars.forEach((color, index) => {
    ctx.fillStyle = color;
    ctx.fillRect(Math.floor(index * barWidth), 0, Math.ceil(barWidth), barHeight);
  });

  // A sweep line crossing the bars once every 60 frames.
  const run = ((frame.tick % 60) / 60) * width;
  const x = frame.mirror ? width - run : run;
  ctx.strokeStyle = colors.sweep;
  ctx.lineWidth = Math.max(2, width / 160);
  ctx.beginPath();
  ctx.moveTo(x, 0);
  ctx.lineTo(x, barHeight);
  ctx.stroke();

  // A centre ring like a lens target.
  const radius = Math.min(width, barHeight) * 0.18;
  ctx.beginPath();
  ctx.arc(width / 2, barHeight / 2, radius, 0, Math.PI * 2);
  ctx.stroke();

  // The caption: clock, label, position.
  ctx.fillStyle = colors.ink;
  ctx.fillRect(0, barHeight, width, height - barHeight);
  ctx.fillStyle = colors.inkInverse;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  const band = height - barHeight;
  const big = Math.max(12, Math.round(band * 0.3));
  const small = Math.max(10, Math.round(band * 0.16));
  ctx.font = `600 ${big}px monospace`;
  ctx.fillText(clockText(frame.now), width / 2, barHeight + band * 0.3);
  ctx.font = `${small}px monospace`;
  ctx.fillText(frame.label, width / 2, barHeight + band * 0.58);
  if (frame.position) ctx.fillText(frame.position, width / 2, barHeight + band * 0.8);
}

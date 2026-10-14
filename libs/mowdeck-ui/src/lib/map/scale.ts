/** The scale bar of the maps: the longest 1-2-5 length that fits in `maxPx`. */
export function scaleFor(metresPerPx: number, maxPx = 96): { width: number; label: string } {
  if (!Number.isFinite(metresPerPx) || metresPerPx <= 0) return { width: 0, label: '' };
  const max = metresPerPx * maxPx;
  const pow = 10 ** Math.floor(Math.log10(max));
  const nice = [5, 2, 1].map((f) => f * pow).find((v) => v <= max) ?? pow;
  const label = nice >= 1000 ? `${nice / 1000} km` : nice >= 1 ? `${nice} m` : `${nice * 100} cm`;
  return { width: Math.round(nice / metresPerPx), label };
}

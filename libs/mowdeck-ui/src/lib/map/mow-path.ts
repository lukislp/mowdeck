/**
 * The mower's planned mow path on the map: the route it computed for the chosen areas and
 * pattern (`MowPath` of the contracts), split at its progress into the part already mowed and
 * the part still ahead. Pure functions, unit tested without a DOM (`src/tests/mow-path.test.ts`).
 *
 * The progress `index` counts points over all polylines in order: points whose flat index is
 * below it are done. A polyline cut by the progress is split there; the done part ends at the
 * cut and the rest starts at the last done point, so both lines meet without a gap.
 */
import type { MapCoord, Projection } from './projection';

type Point = readonly number[];

/** The mow path (`MowPath` of the contracts) as the map reads it: local metres. */
export interface MapMowPath {
  path_hash: string;
  polylines?: readonly (readonly Point[])[];
  kinds?: readonly string[] | null;
  points_total: number;
  progress?: { index: number; direction?: number } | null;
}

/** One drawable piece of the path: at least two points, done or still ahead. */
export interface PathPiece<P = Point> {
  points: P[];
  done: boolean;
  /** The vendor's kind of the polyline (`mow`, `edge`, `transfer`, `other`). */
  kind: string;
}

/** The path split at ``index`` (flat point count); pieces shorter than two points are dropped. */
export function splitPathProgress(
  polylines: readonly (readonly Point[])[],
  index: number | null | undefined,
  kinds?: readonly string[] | null,
): PathPiece[] {
  const pieces: PathPiece[] = [];
  const cut = index == null ? 0 : Math.max(0, Math.floor(index));
  let offset = 0;
  polylines.forEach((line, i) => {
    const kind = kinds?.[i] ?? 'mow';
    const local = cut - offset;
    offset += line.length;
    if (local >= line.length) {
      pieces.push({ points: [...line], done: true, kind });
    } else if (local <= 0) {
      pieces.push({ points: [...line], done: false, kind });
    } else {
      pieces.push({ points: line.slice(0, local), done: true, kind });
      pieces.push({ points: line.slice(local - 1), done: false, kind });
    }
  });
  return pieces.filter((piece) => piece.points.length >= 2);
}

/** Share of the path done, 0 to 1 (0 without progress or points). */
export function pathDoneShare(path: MapMowPath | null | undefined): number {
  if (!path || path.points_total <= 0 || !path.progress) return 0;
  return Math.min(1, Math.max(0, path.progress.index / path.points_total));
}

/** The pieces of ``path`` in map coordinates, ready for Leaflet polylines. */
export function pathLayers(
  path: MapMowPath | null | undefined,
  projection: Projection,
): PathPiece<MapCoord>[] {
  if (!path) return [];
  return splitPathProgress(path.polylines ?? [], path.progress?.index, path.kinds).map((piece) => ({
    ...piece,
    points: piece.points.map((p) => projection.fromLocal(p[0] ?? 0, p[1] ?? 0)),
  }));
}

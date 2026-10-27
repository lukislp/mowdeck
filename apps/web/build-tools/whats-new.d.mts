export const MAX_RELEASES: number;
export interface ParsedSubject {
    type: string;
    scope: string | null;
    breaking: boolean;
    text: string;
    pr: number | null;
}
export interface Curated {
    prs: number[];
    hide: boolean;
    de?: string;
    en?: string;
    href?: string;
}
export interface RawRelease {
    version: string;
    date: string;
    commits: string[];
}
export interface OutEntry {
    id: string;
    kind: 'feature' | 'fix';
    en: string;
    de?: string;
    href?: string;
}
export interface OutRelease {
    version: string;
    date: string;
    entries: OutEntry[];
}
export function parseSubject(subject: string): ParsedSubject | null;
export function isUserFacing(parsed: ParsedSubject | null): boolean;
export function subjectText(text: string): string;
export function parseCurated(raw: unknown, file?: string): Curated;
export function loadCurated(dir: string): Curated[];
export function buildReleases(
    releases: RawRelease[],
    curated: Curated[],
    max?: number,
): OutRelease[];
export function readReleases(version?: string, cwd?: string): RawRelease[];

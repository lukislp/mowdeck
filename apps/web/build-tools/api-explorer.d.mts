import type { Plugin } from 'vite';

export const EXPLORER_BASE: string;
export const SCALAR_PACKAGE: string;
export function integrity(bytes: string | Uint8Array): string;
export function scalarPackage(webRoot?: string): { dir: string; version: string; license: string };
export function pinnedVersion(webRoot?: string): string;
export function explorerDocuments(
    openapiDir: string,
): { slug: string; title: string; document: Record<string, unknown> }[];
export function explorerFiles(webRoot?: string): Map<string, Uint8Array | string>;
export function writeExplorer(outDir: string, webRoot?: string): { target: string; count: number };
export function apiExplorerDev(webRoot?: string): Plugin;

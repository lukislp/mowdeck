// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

// The app is a static SPA: no server rendering, nothing prerendered; adapter-static writes the
// 200.html fallback that nginx serves for every route.
export const ssr = false;
export const prerender = false;
export const trailingSlash = 'never';

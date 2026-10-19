import { describe, expect, it } from 'vitest';

import {
  agoraIncluded,
  bundleProblems,
  isAgoraPackage,
  licenceAllowed,
  noticeText,
  packageOfModule,
  packageOfSpecifier,
} from './release-licences';

describe('release licence guard of the web bundle', () => {
  it('recognises the Agora packages', () => {
    expect(isAgoraPackage('agora-rtc-sdk-ng')).toBe(true);
    expect(isAgoraPackage('@agora-js/media')).toBe(true);
    expect(isAgoraPackage('agora-rte-extension')).toBe(true);
    expect(isAgoraPackage('agora')).toBe(true);
    expect(isAgoraPackage('svelte')).toBe(false);
    expect(isAgoraPackage('pagora')).toBe(false);
  });

  it('extracts package names from specifiers and module ids', () => {
    expect(packageOfSpecifier('agora-rtc-sdk-ng/esm')).toBe('agora-rtc-sdk-ng');
    expect(packageOfSpecifier('@agora-js/shared')).toBe('@agora-js/shared');
    expect(packageOfSpecifier('./agora-player')).toBeNull();
    expect(packageOfSpecifier('\0virtual')).toBeNull();
    expect(
      packageOfModule(
        'C:\\repo\\node_modules\\.pnpm\\agora-rtc-sdk-ng@4.24.8\\node_modules\\agora-rtc-sdk-ng\\AgoraRTC_N-production.js',
      ),
    ).toEqual({
      root: 'C:/repo/node_modules/.pnpm/agora-rtc-sdk-ng@4.24.8/node_modules/agora-rtc-sdk-ng',
      name: 'agora-rtc-sdk-ng',
    });
    expect(packageOfModule('/r/node_modules/@sveltejs/kit/src/runtime/client.js?v=1')?.name).toBe(
      '@sveltejs/kit',
    );
    expect(packageOfModule('/r/apps/web/src/lib/config.ts')).toBeNull();
    expect(packageOfModule('\0vite/preload-helper.js')).toBeNull();
  });

  it('accepts permissive SPDX expressions only', () => {
    expect(licenceAllowed('MIT')).toBe(true);
    expect(licenceAllowed('(MIT OR GPL-3.0-only)')).toBe(true);
    expect(licenceAllowed('MIT AND ISC')).toBe(true);
    expect(licenceAllowed('MIT AND GPL-3.0-only')).toBe(false);
    expect(licenceAllowed('SEE LICENSE IN LICENSE.md')).toBe(false);
    expect(licenceAllowed('UNLICENSED')).toBe(false);
    expect(licenceAllowed(null)).toBe(false);
  });

  it('refuses Agora packages unless the SDK is included', () => {
    const packages = [
      { name: 'svelte', version: '5', license: 'MIT', licenseText: 'MIT' },
      { name: 'agora-rtc-sdk-ng', version: '4.24.8', license: 'MIT', licenseText: null },
      { name: 'closed', version: '1', license: 'Proprietary', licenseText: null },
    ];
    expect(bundleProblems(packages, false)).toEqual([
      'agora-rtc-sdk-ng@4.24.8: Agora package in the bundle',
      'closed@1: licence Proprietary not allowed',
    ]);
    expect(bundleProblems(packages, true)).toEqual(['closed@1: licence Proprietary not allowed']);
    expect(noticeText(packages.slice(0, 1))).toContain('svelte@5 (MIT)');
  });

  it('reads the build switch strictly', () => {
    expect(agoraIncluded(undefined)).toBe(false);
    expect(agoraIncluded('exclude')).toBe(false);
    expect(agoraIncluded('include')).toBe(true);
    expect(() => agoraIncluded('yes')).toThrow(/MOWDECK_WEB_AGORA_SDK/);
  });
});

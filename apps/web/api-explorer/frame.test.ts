import { describe, expect, it } from 'vitest';

import {
  READY_MESSAGE,
  SECURITY_SCHEME,
  TOKEN_MESSAGE,
  parseIndex,
  prepareDocument,
  scalarConfiguration,
  tokenFromMessage,
} from './frame.js';

const ORIGIN = 'https://demo.example.org';
const parent = {};
const self = { origin: ORIGIN, parent };

describe('tokenFromMessage', () => {
  const message = (data: unknown, origin = ORIGIN, source: unknown = parent) => ({
    origin,
    source,
    data,
  });

  it('takes the token from the embedding page of the same origin', () => {
    expect(tokenFromMessage(message({ type: TOKEN_MESSAGE, token: 'eyJ.a-b_c.d' }), self)).toBe(
      'eyJ.a-b_c.d',
    );
    expect(tokenFromMessage(message({ type: TOKEN_MESSAGE, token: null }), self)).toBeNull();
  });

  it('ignores other origins, other windows and other messages', () => {
    const data = { type: TOKEN_MESSAGE, token: 'abc' };
    expect(tokenFromMessage(message(data, 'https://evil.example'), self)).toBeUndefined();
    expect(tokenFromMessage(message(data, ORIGIN, {}), self)).toBeUndefined();
    expect(tokenFromMessage(message(data, ORIGIN, null), { origin: ORIGIN, parent: null })).toBe(
      undefined,
    );
    expect(tokenFromMessage(message({ type: READY_MESSAGE }), self)).toBeUndefined();
    expect(tokenFromMessage(message('abc'), self)).toBeUndefined();
    expect(tokenFromMessage(message(null), self)).toBeUndefined();
  });

  it('refuses a token that cannot be a JWT', () => {
    for (const token of ['', 'a b', 'x"y', 42, 'a'.repeat(9000)]) {
      expect(tokenFromMessage(message({ type: TOKEN_MESSAGE, token }), self)).toBeUndefined();
    }
  });
});

describe('prepareDocument', () => {
  const document = {
    openapi: '3.1.0',
    info: { title: 'fleet', version: '0.1.0' },
    paths: {
      '/.well-known/mowdeck-module': { get: {} },
      '/api/v1/fleet/mowers': { get: {} },
    },
    components: { schemas: { Mower: { type: 'object' } } },
  };

  it('keeps the public API, this origin as server and the session as security', () => {
    const prepared = prepareDocument(document, ORIGIN);
    expect(Object.keys(prepared.paths)).toEqual(['/api/v1/fleet/mowers']);
    expect(prepared.servers).toEqual([{ url: ORIGIN, description: 'This installation' }]);
    expect(prepared.security).toEqual([{ [SECURITY_SCHEME]: [] }]);
    expect(prepared.components.securitySchemes[SECURITY_SCHEME]).toMatchObject({
      type: 'http',
      scheme: 'bearer',
    });
    expect(prepared.components.schemas).toEqual(document.components.schemas);
  });

  it('leaves the input as it was', () => {
    const before = JSON.stringify(document);
    prepareDocument(document, ORIGIN);
    expect(JSON.stringify(document)).toBe(before);
  });

  it('copes with a document without paths or components', () => {
    const prepared = prepareDocument({ openapi: '3.1.0' }, ORIGIN);
    expect(prepared.paths).toEqual({});
    expect(Object.keys(prepared.components.securitySchemes)).toEqual([SECURITY_SCHEME]);
  });
});

describe('scalarConfiguration', () => {
  const sources = [
    { slug: 'fleet', title: 'Fleet', content: {} },
    { slug: 'telemetry', title: 'Telemetry', content: {} },
  ];

  it('talks to nobody but this origin', () => {
    const config = scalarConfiguration({ sources, token: 't', dark: false }, ORIGIN);
    expect(config).toMatchObject({
      withDefaultFonts: false,
      telemetry: false,
      agent: { disabled: true },
      mcp: { disabled: true },
      hideClientButton: true,
      showDeveloperTools: 'never',
      persistAuth: false,
    });
    expect(config).not.toHaveProperty('proxyUrl');
    for (const url of Object.values(config.externalUrls)) {
      expect(new URL(url).origin).toBe(ORIGIN);
    }
    expect(JSON.stringify(config)).not.toMatch(/scalar\.com|https?:\/\/(?!demo\.example\.org)/);
  });

  it('fills in the session token and marks the first document as default', () => {
    const config = scalarConfiguration({ sources, token: 'abc', dark: true }, ORIGIN);
    expect(config.authentication).toEqual({
      preferredSecurityScheme: SECURITY_SCHEME,
      securitySchemes: { [SECURITY_SCHEME]: { token: 'abc' } },
    });
    expect(config.sources.map((s) => [s.slug, s.default])).toEqual([
      ['fleet', true],
      ['telemetry', false],
    ]);
    expect(config.darkMode).toBe(true);
    const signedOut = scalarConfiguration({ sources, token: null, dark: false }, ORIGIN);
    expect(signedOut.authentication.securitySchemes[SECURITY_SCHEME].token).toBe('');
  });
});

describe('parseIndex', () => {
  it('keeps well-formed entries only', () => {
    expect(
      parseIndex([
        { slug: 'fleet', title: 'Fleet' },
        { slug: '../x', title: 'X' },
        { slug: 'device-gateway', title: 'Device Gateway' },
        { slug: 'vision' },
        null,
      ]),
    ).toEqual([
      { slug: 'fleet', title: 'Fleet' },
      { slug: 'device-gateway', title: 'Device Gateway' },
    ]);
    expect(parseIndex({})).toEqual([]);
  });
});

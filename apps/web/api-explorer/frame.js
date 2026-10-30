/**
 * The API explorer's frame (ADR 0072), pure part: the message protocol with the web app, the
 * preparation of the services' OpenAPI documents and the Scalar configuration. No DOM access here,
 * so the unit tests cover it (frame.test.ts); `explorer.js` wires it to the page.
 *
 * The frame never signs in by itself. The web app's page `/api-explorer` embeds it and posts the
 * access token of the visitor's session (and every renewed one); the frame passes it to Scalar,
 * which sends "try it" requests to `/api/v1` on the same origin. The token stays in memory.
 */

/** Message type of the web app: the current access token (or null after a logout). */
export const TOKEN_MESSAGE = 'mowdeck-explorer-token';
/** Message type of the frame: loaded, send the token. */
export const READY_MESSAGE = 'mowdeck-explorer-ready';
/** Name of the security scheme the frame adds to every document. */
export const SECURITY_SCHEME = 'mowdeckSession';

/**
 * The token of a message from the web app, or undefined when the message is not one. Only a
 * message from the same origin and from the embedding page counts.
 *
 * @param {{ origin: string; source: unknown; data: unknown }} event
 * @param {{ origin: string; parent: unknown }} self
 * @returns {string | null | undefined}
 */
export function tokenFromMessage(event, self) {
  if (event.origin !== self.origin || event.source !== self.parent || event.source === null) {
    return undefined;
  }
  const data = /** @type {{ type?: unknown; token?: unknown } | null} */ (event.data);
  if (!data || typeof data !== 'object' || data.type !== TOKEN_MESSAGE) return undefined;
  if (data.token === null) return null;
  return typeof data.token === 'string' && /^[\w.-]{1,8192}$/.test(data.token)
    ? data.token
    : undefined;
}

/**
 * A service's OpenAPI document as the explorer shows it: only the public API under `/api/v1`
 * (the gateway routes nothing else), the installation's own origin as the one server, and the
 * session's Bearer token as the security of every operation. The input is not changed.
 *
 * @param {Record<string, any>} document
 * @param {string} origin
 * @returns {Record<string, any>}
 */
export function prepareDocument(document, origin) {
  const paths = Object.fromEntries(
    Object.entries(document.paths ?? {}).filter(([path]) => path.startsWith('/api/v1/')),
  );
  return {
    ...document,
    servers: [{ url: origin, description: 'This installation' }],
    paths,
    components: {
      ...document.components,
      securitySchemes: {
        ...document.components?.securitySchemes,
        [SECURITY_SCHEME]: {
          type: 'http',
          scheme: 'bearer',
          bearerFormat: 'JWT',
          description:
            'The access token of your session in the web app, filled in for you. Scripts use a personal access token instead (see the scripting guide).',
        },
      },
    },
    security: [{ [SECURITY_SCHEME]: [] }],
  };
}

/**
 * Where an outbound feature of Scalar would go: nowhere outside this installation. The frame's
 * own path answers 404 for it, and the CSP (connect-src 'self') blocks any other host anyway.
 *
 * @param {string} origin
 */
export function localExternalUrls(origin) {
  const nowhere = `${origin}/_explorer/offline`;
  return {
    dashboardUrl: nowhere,
    registryUrl: nowhere,
    proxyUrl: nowhere,
    apiBaseUrl: nowhere,
  };
}

/**
 * The Scalar configuration. Privacy (ADR 0072): no default fonts (fonts.scalar.com), no request
 * proxy (proxy.scalar.com; requests go straight to the same origin), no telemetry, no AI agent,
 * no MCP, no "open in the API client" and no developer tools (both link to scalar.com), every
 * external Scalar service pointed at this installation, and the token never persisted.
 *
 * @param {{
 *   sources: { slug: string; title: string; content: Record<string, any> }[];
 *   token: string | null;
 *   dark: boolean;
 * }} options
 * @param {string} origin
 */
export function scalarConfiguration({ sources, token, dark }, origin) {
  return {
    sources: sources.map((source, index) => ({
      slug: source.slug,
      title: source.title,
      content: source.content,
      default: index === 0,
    })),
    authentication: {
      preferredSecurityScheme: SECURITY_SCHEME,
      securitySchemes: { [SECURITY_SCHEME]: { token: token ?? '' } },
    },
    persistAuth: false,
    withDefaultFonts: false,
    telemetry: false,
    agent: { disabled: true },
    mcp: { disabled: true },
    hideClientButton: true,
    showDeveloperTools: 'never',
    externalUrls: localExternalUrls(origin),
    darkMode: dark,
    hideDarkModeToggle: true,
    documentDownloadType: 'json',
    defaultOpenFirstTag: true,
    layout: 'modern',
    // Scalar's own theme; explorer.css sets MowDeck's fonts and accent on top of it.
    theme: 'default',
  };
}

/**
 * The list of documents the build ships (`openapi/index.json`): `{ slug, title }` entries.
 *
 * @param {unknown} raw
 * @returns {{ slug: string; title: string }[]}
 */
export function parseIndex(raw) {
  if (!Array.isArray(raw)) return [];
  return raw.filter(
    (entry) =>
      entry &&
      typeof entry === 'object' &&
      typeof entry.slug === 'string' &&
      /^[a-z][a-z-]*$/.test(entry.slug) &&
      typeof entry.title === 'string',
  );
}

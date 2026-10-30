/**
 * The web app's side of the API explorer (ADR 0072). The page `/api-explorer` embeds the frame
 * `/_explorer/index.html` (pinned Scalar bundle with its own CSP, served by nginx only with
 * `MOWDECK_WEB_API_EXPLORER=true`) and hands it the access token of the current session:
 *
 * - the frame posts `ready` once it listens; the page answers with the token;
 * - every renewed token (the session refreshes 60 s before expiry) and the logout (null) follow;
 * - messages go to this origin only and are accepted only from the embedded frame.
 *
 * No new credential: the frame acts with exactly the session the visitor already has, and never
 * stores the token. In the demo every action reaches only simulated mowers or is refused (403
 * `demo-mode`, ADR 0068).
 */

/** Path of the frame page; the message names below match `api-explorer/frame.js`. */
export const EXPLORER_FRAME = '/_explorer/index.html';
export const TOKEN_MESSAGE = 'mowdeck-explorer-token';
export const READY_MESSAGE = 'mowdeck-explorer-ready';

export interface TokenSource {
  readonly token: string | null;
  onToken(listener: (token: string | null) => void): () => void;
}

interface FrameLike {
  readonly contentWindow: Pick<Window, 'postMessage'> | null;
}

interface WindowLike {
  readonly location: { readonly origin: string };
  addEventListener(type: 'message', listener: (event: MessageEvent) => void): void;
  removeEventListener(type: 'message', listener: (event: MessageEvent) => void): void;
}

/** The frame's address with the app's colour scheme (the frame has no access to the app's). */
export function frameUrl(dark: boolean): string {
  return `${EXPLORER_FRAME}?theme=${dark ? 'dark' : 'light'}`;
}

/**
 * Hand the session's token to the frame, now and on every change. Returns the function that
 * stops it (the page calls it when it is left).
 */
export function connectFrame(
  frame: FrameLike,
  session: TokenSource,
  win: WindowLike = window,
): () => void {
  const origin = win.location.origin;
  let ready = false;
  const send = (token: string | null) => {
    if (!ready) return;
    frame.contentWindow?.postMessage({ type: TOKEN_MESSAGE, token }, origin);
  };
  const onMessage = (event: MessageEvent) => {
    if (event.origin !== origin || event.source === null) return;
    if (event.source !== frame.contentWindow) return;
    const data = event.data as { type?: unknown } | null;
    if (data?.type !== READY_MESSAGE) return;
    ready = true;
    send(session.token);
  };
  win.addEventListener('message', onMessage);
  const stop = session.onToken(send);
  return () => {
    win.removeEventListener('message', onMessage);
    stop();
  };
}

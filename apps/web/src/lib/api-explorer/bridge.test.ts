import { describe, expect, it, vi } from 'vitest';

import * as frame from '../../../api-explorer/frame.js';

import {
  EXPLORER_FRAME,
  READY_MESSAGE,
  TOKEN_MESSAGE,
  connectFrame,
  frameUrl,
  type TokenSource,
} from './bridge';

const ORIGIN = 'https://demo.example.org';

function setup(initial: string | null = 'first') {
  const listeners = new Set<(event: MessageEvent) => void>();
  const win = {
    location: { origin: ORIGIN },
    addEventListener: (_: 'message', listener: (event: MessageEvent) => void) =>
      listeners.add(listener),
    removeEventListener: (_: 'message', listener: (event: MessageEvent) => void) =>
      listeners.delete(listener),
  };
  const contentWindow = { postMessage: vi.fn() };
  const tokenListeners = new Set<(token: string | null) => void>();
  const session: TokenSource & { emit(token: string | null): void } = {
    token: initial,
    onToken(listener) {
      tokenListeners.add(listener);
      return () => tokenListeners.delete(listener);
    },
    emit(token) {
      for (const listener of tokenListeners) listener(token);
    },
  };
  const deliver = (data: unknown, origin = ORIGIN, source: unknown = contentWindow) => {
    for (const listener of listeners) {
      listener({ data, origin, source } as unknown as MessageEvent);
    }
  };
  const stop = connectFrame({ contentWindow }, session, win);
  return { contentWindow, session, deliver, stop, listeners, tokenListeners };
}

describe('connectFrame', () => {
  it('sends the token once the frame is ready, to this origin only', () => {
    const { contentWindow, deliver } = setup();
    expect(contentWindow.postMessage).not.toHaveBeenCalled();
    deliver({ type: READY_MESSAGE });
    expect(contentWindow.postMessage).toHaveBeenCalledWith(
      { type: TOKEN_MESSAGE, token: 'first' },
      ORIGIN,
    );
  });

  it('forwards renewed tokens and the logout', () => {
    const { contentWindow, deliver, session } = setup();
    session.emit('early');
    expect(contentWindow.postMessage).not.toHaveBeenCalled();
    deliver({ type: READY_MESSAGE });
    session.emit('second');
    session.emit(null);
    expect(contentWindow.postMessage.mock.calls.map(([message]) => message.token)).toEqual([
      'first',
      'second',
      null,
    ]);
  });

  it('ignores messages of other origins and other windows', () => {
    const { contentWindow, deliver } = setup();
    deliver({ type: READY_MESSAGE }, 'https://evil.example');
    deliver({ type: READY_MESSAGE }, ORIGIN, {});
    deliver({ type: READY_MESSAGE }, ORIGIN, null);
    deliver({ type: 'other' });
    deliver(null);
    expect(contentWindow.postMessage).not.toHaveBeenCalled();
  });

  it('lets go of the window and the session when stopped', () => {
    const { stop, listeners, tokenListeners } = setup();
    stop();
    expect(listeners.size).toBe(0);
    expect(tokenListeners.size).toBe(0);
  });
});

describe('frame protocol', () => {
  it('uses the same message names as the frame', () => {
    expect(TOKEN_MESSAGE).toBe(frame.TOKEN_MESSAGE);
    expect(READY_MESSAGE).toBe(frame.READY_MESSAGE);
  });

  it('passes the colour scheme to the frame', () => {
    expect(frameUrl(true)).toBe(`${EXPLORER_FRAME}?theme=dark`);
    expect(frameUrl(false)).toBe(`${EXPLORER_FRAME}?theme=light`);
  });
});

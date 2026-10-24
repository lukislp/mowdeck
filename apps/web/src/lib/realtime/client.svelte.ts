/**
 * The Realtime connection of this tab (ADR 0018 protocol, design section 5).
 *
 * One socket per tab, created by the first `watch`. Subscriptions are reference counted per
 * mower; a release is debounced by 2 s so list -> detail -> list does not resubscribe. The token
 * travels in the first message (`auth`) and again after every refresh (`reauth`). Close codes
 * decide what happens next: 4401 gets a fresh token, 4429 backs off from 30 s, everything else
 * reconnects with full jitter backoff. An application ping every 25 s detects half-open sockets.
 *
 * Manual driving (ADR 0026) uses the same socket: `startDrive` opens a session, `sendFrame` sends
 * joystick frames (rate limited here to the server's `max_rate_hz`, so excess frames are dropped
 * before they cost bandwidth), `stopDrive` ends it. The client keeps no motion state: when the
 * socket closes the session counts as ended (`connection_lost`) and no frame can be sent, so the
 * Device-Gateway's dead-man timer stops the mower within `ttl_ms`.
 */
import type {
  CommandResultMessage,
  DriveCapabilities,
  DriveEndReason,
  RealtimeClientMessage,
  RealtimeErrorCode,
  RealtimeServerMessage,
  StateMessage,
} from '#lib/generated/realtime.js';

/** An open drive session as `drive_started` announced it. */
export interface DriveSession {
  sessionId: string;
  mowerId: string;
  maxRateHz: number;
  ttlMs: number;
  maxSeconds: number;
  bladeOnAllowed: boolean;
  capabilities: DriveCapabilities;
  startedAt: number;
}

/** Why a session ended: the server's reasons, or the socket went away under it. */
export type DriveEnd = DriveEndReason | 'connection_lost';

export type DriveEvent =
  | { type: 'started'; session: DriveSession }
  | { type: 'ack'; sessionId: string; seq: number }
  | { type: 'ended'; sessionId: string; reason: DriveEnd; detail: string | null }
  | { type: 'error'; code: RealtimeErrorCode; message: string };

export interface DriveFrame {
  linear: number;
  angular: number;
  blade_on?: boolean;
}

export type Phase = 'idle' | 'connecting' | 'authenticating' | 'ready' | 'backoff' | 'stopped';

export interface SocketLike {
  readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent<string>) => void) | null;
  onclose: ((event: CloseEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
}

export interface RealtimeDeps {
  url: () => string;
  /**
   * A valid access token; `force` asks for a refresh (`rejected`: the token the server refused,
   * so one that replaced it meanwhile is used without another refresh). Null: the session is
   * gone.
   */
  getToken(force?: boolean, rejected?: string | null): Promise<string | null>;
  onState(message: StateMessage): void;
  onResult(message: CommandResultMessage): void;
  onRevoked(mowerIds: string[]): void;
  onDenied?(mowerIds: string[]): void;
  onSessionLost(): void;
  socketFactory?: (url: string) => SocketLike;
  random?: () => number;
  /** Milliseconds clock for the frame rate limit (tests inject one). */
  now?: () => number;
}

export const SUBSCRIBE_CHUNK = 200;
export const RELEASE_DELAY_MS = 2_000;
export const PING_INTERVAL_MS = 25_000;
export const PONG_TIMEOUT_MS = 10_000;
export const BACKOFF_BASE_MS = 500;
export const BACKOFF_MAX_MS = 30_000;
export const TOO_MANY_MIN_MS = 30_000;
/** A frame may come this much earlier than 1 / max_rate_hz (timer jitter). */
export const FRAME_SLACK = 0.9;
const OPEN = 1;

/** Full jitter: random(0, min(max, base * 2^attempt)). */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  return Math.floor(random() * Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** attempt));
}

export class RealtimeClient {
  phase = $state<Phase>('idle');
  /** The server refused another socket for this user (close 4429). */
  tooMany = $state(false);

  #deps: RealtimeDeps;
  #ws: SocketLike | null = null;
  #refs = new Map<string, number>();
  #attempt = 0;
  #retryTimer: ReturnType<typeof setTimeout> | undefined;
  #pingTimer: ReturnType<typeof setInterval> | undefined;
  #pongTimer: ReturnType<typeof setTimeout> | undefined;
  #requests = 0;
  /** The token the socket authenticated with last. */
  #authToken: string | null = null;

  /** The open drive session of this socket, if any. */
  drive = $state.raw<DriveSession | null>(null);
  /** A `drive_start` waits for its answer. */
  driveStarting = $state(false);
  #drivePending: { requestId: string; mowerId: string } | null = null;
  #driveListeners = new Set<(event: DriveEvent) => void>();
  #seq = 0;
  #lastFrameAt = -Infinity;

  constructor(deps: RealtimeDeps) {
    this.#deps = deps;
  }

  // --- manual driving ---------------------------------------------------------------------------

  /** Listen to drive events; returns the unsubscribe function. */
  onDrive(listener: (event: DriveEvent) => void): () => void {
    this.#driveListeners.add(listener);
    return () => {
      this.#driveListeners.delete(listener);
    };
  }

  /** Ask for a drive session; false when the socket is not ready. */
  startDrive(mowerId: string, bladeOnAllowed = false): boolean {
    if (this.phase !== 'ready' || !this.#isOpen()) return false;
    const requestId = this.#requestId();
    this.#setPending({ requestId, mowerId });
    this.#send({
      type: 'drive_start',
      request_id: requestId,
      mower_id: mowerId,
      blade_on_allowed: bladeOnAllowed,
    });
    return true;
  }

  /**
   * Send one joystick frame of the open session. Returns false when there is no session, the
   * socket is closed, or the frame comes sooner than the server's rate allows (dropped here);
   * `force` skips the rate check (the final zero frame when the stick is released).
   */
  sendFrame(frame: DriveFrame, force = false): boolean {
    const session = this.drive;
    if (!session || !this.#isOpen()) return false;
    const now = this.#now();
    const gap = 1000 / Math.max(1, session.maxRateHz);
    if (!force && now - this.#lastFrameAt < gap * FRAME_SLACK) return false;
    this.#lastFrameAt = now;
    this.#seq += 1;
    this.#send({
      type: 'drive_frame',
      session_id: session.sessionId,
      seq: this.#seq,
      linear: clampUnit(frame.linear),
      angular: clampUnit(frame.angular),
      blade_on: session.bladeOnAllowed && frame.blade_on === true,
    });
    return true;
  }

  /** End the session (or forget a pending start); the server sends a zero frame at once. */
  stopDrive(): void {
    this.#setPending(null);
    const session = this.drive;
    if (!session) return;
    this.drive = null;
    this.#send({
      type: 'drive_stop',
      session_id: session.sessionId,
      request_id: this.#requestId(),
    });
    this.#emitDrive({
      type: 'ended',
      sessionId: session.sessionId,
      reason: 'client_stop',
      detail: null,
    });
  }

  #setPending(pending: { requestId: string; mowerId: string } | null): void {
    this.#drivePending = pending;
    this.driveStarting = pending !== null;
  }

  #onDriveMessage(message: RealtimeServerMessage): void {
    switch (message.type) {
      case 'drive_started': {
        const pending = this.#drivePending;
        const matches =
          pending !== null &&
          (message.request_id == null || message.request_id === pending.requestId);
        if (!matches) {
          // Nobody wants this session any more (the page was left): close it at once.
          this.#send({ type: 'drive_stop', session_id: message.session_id });
          return;
        }
        this.#setPending(null);
        this.#seq = 0;
        this.#lastFrameAt = -Infinity;
        const session: DriveSession = {
          sessionId: message.session_id,
          mowerId: String(message.mower_id),
          maxRateHz: message.max_rate_hz,
          ttlMs: message.ttl_ms,
          maxSeconds: message.max_seconds,
          bladeOnAllowed: message.blade_on_allowed,
          capabilities: message.capabilities,
          startedAt: this.#now(),
        };
        this.drive = session;
        this.#emitDrive({ type: 'started', session });
        break;
      }
      case 'drive_frame_ack':
        if (this.drive?.sessionId === message.session_id) {
          this.#emitDrive({ type: 'ack', sessionId: message.session_id, seq: message.seq });
        }
        break;
      case 'drive_ended':
        if (this.drive?.sessionId === message.session_id) {
          this.drive = null;
          this.#emitDrive({
            type: 'ended',
            sessionId: message.session_id,
            reason: message.reason,
            detail: message.detail ?? null,
          });
        }
        break;
      default:
        break;
    }
  }

  #onDriveError(code: RealtimeErrorCode, message: string, requestId: string | null): void {
    const pending = this.#drivePending;
    if (pending && (requestId === null || requestId === pending.requestId)) {
      this.#setPending(null);
      this.#emitDrive({ type: 'error', code, message });
      return;
    }
    if (code === 'drive_not_active' && this.drive) {
      const session = this.drive;
      this.drive = null;
      this.#emitDrive({ type: 'error', code, message });
      this.#emitDrive({
        type: 'ended',
        sessionId: session.sessionId,
        reason: 'connection_lost',
        detail: message,
      });
    }
  }

  /** The socket is gone: no frame can be sent, the dead-man timer stops the mower. */
  #loseDrive(): void {
    if (this.#drivePending) {
      this.#setPending(null);
      this.#emitDrive({ type: 'error', code: 'drive_unavailable', message: 'connection lost' });
    }
    const session = this.drive;
    if (!session) return;
    this.drive = null;
    this.#emitDrive({
      type: 'ended',
      sessionId: session.sessionId,
      reason: 'connection_lost',
      detail: null,
    });
  }

  #emitDrive(event: DriveEvent): void {
    for (const listener of [...this.#driveListeners]) listener(event);
  }

  #now(): number {
    return (this.#deps.now ?? Date.now)();
  }

  #isOpen(): boolean {
    return this.#ws !== null && this.#ws.readyState === OPEN;
  }

  /** Ids with at least one holder (what the socket is subscribed to). */
  get watched(): string[] {
    return [...this.#refs.keys()];
  }

  /** Subscribe `ids` for as long as the returned release function is not called. */
  watch(ids: string[]): () => void {
    const added: string[] = [];
    for (const id of ids) {
      const count = this.#refs.get(id) ?? 0;
      this.#refs.set(id, count + 1);
      if (count === 0) added.push(id);
    }
    if (this.phase === 'idle' || this.phase === 'stopped') this.#connect();
    else if (this.phase === 'ready' && added.length) this.#subscribe(added);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      setTimeout(() => this.#release(ids), RELEASE_DELAY_MS);
    };
  }

  /** Send a new token on the open socket (after every refresh). */
  reauth(token: string): void {
    if (this.#ws && this.#ws.readyState === OPEN && this.phase !== 'connecting') {
      this.#authToken = token;
      this.#send({ type: 'auth', token });
    }
  }

  /** Reconnect now when waiting (network back, tab visible again). */
  wake(): void {
    if (this.phase === 'backoff') {
      clearTimeout(this.#retryTimer);
      this.#connect();
    }
  }

  /** Logout: close for good and forget the subscriptions. */
  stop(): void {
    if (this.drive) this.#send({ type: 'drive_stop', session_id: this.drive.sessionId });
    this.phase = 'stopped';
    this.#refs.clear();
    this.#clearTimers();
    const ws = this.#ws;
    this.#ws = null;
    ws?.close(1000, 'logout');
    this.#loseDrive();
  }

  #release(ids: string[]): void {
    const removed: string[] = [];
    for (const id of ids) {
      const count = this.#refs.get(id) ?? 0;
      if (count <= 1) {
        if (this.#refs.delete(id)) removed.push(id);
      } else {
        this.#refs.set(id, count - 1);
      }
    }
    if (removed.length && this.phase === 'ready') {
      this.#send({
        type: 'unsubscribe',
        mower_ids: removed as [string, ...string[]],
        request_id: this.#requestId(),
      });
    }
  }

  #connect(): void {
    this.#clearTimers();
    this.phase = 'connecting';
    const factory =
      this.#deps.socketFactory ?? ((url: string) => new WebSocket(url) as unknown as SocketLike);
    let ws: SocketLike;
    try {
      ws = factory(this.#deps.url());
    } catch {
      this.#scheduleRetry(backoffDelay(this.#attempt++, this.#deps.random));
      return;
    }
    this.#ws = ws;
    ws.onopen = () => void this.#authenticate(ws);
    ws.onmessage = (event) => this.#onMessage(ws, event.data);
    ws.onclose = (event) => void this.#onClose(ws, event.code);
    ws.onerror = () => undefined;
  }

  async #authenticate(ws: SocketLike): Promise<void> {
    if (ws !== this.#ws) return;
    this.phase = 'authenticating';
    let token: string | null;
    try {
      token = await this.#deps.getToken();
    } catch {
      // No token right now (Identity unreachable): try again with backoff, keep the session.
      if (ws !== this.#ws) return;
      this.#ws = null;
      ws.close(4000, 'no token');
      this.#scheduleRetry(backoffDelay(this.#attempt++, this.#deps.random));
      return;
    }
    if (ws !== this.#ws) return;
    if (token === null) {
      this.#deps.onSessionLost();
      this.stop();
      return;
    }
    this.#authToken = token;
    this.#send({ type: 'auth', token });
  }

  #onMessage(ws: SocketLike, data: string): void {
    if (ws !== this.#ws) return;
    let message: RealtimeServerMessage;
    try {
      message = JSON.parse(data) as RealtimeServerMessage;
    } catch {
      return;
    }
    switch (message.type) {
      case 'ready': {
        const first = this.phase !== 'ready';
        this.phase = 'ready';
        this.tooMany = false;
        this.#attempt = 0;
        if (first) {
          this.#subscribe(this.watched);
          this.#startPing();
        }
        break;
      }
      case 'subscribed':
        if (message.denied?.length) this.#deps.onDenied?.(message.denied.map(String));
        break;
      case 'state':
        this.#deps.onState(message);
        break;
      case 'command_result':
        this.#deps.onResult(message);
        break;
      case 'revoked': {
        const ids = message.mower_ids.map(String);
        for (const id of ids) this.#refs.delete(id);
        this.#deps.onRevoked(ids);
        break;
      }
      case 'pong':
        clearTimeout(this.#pongTimer);
        break;
      case 'error':
        if (message.code.startsWith('drive_')) {
          this.#onDriveError(message.code, message.message, message.request_id ?? null);
        }
        break;
      case 'drive_started':
      case 'drive_frame_ack':
      case 'drive_ended':
        this.#onDriveMessage(message);
        break;
      default:
        break;
    }
  }

  async #onClose(ws: SocketLike, code: number): Promise<void> {
    if (ws !== this.#ws) return;
    this.#ws = null;
    this.#clearTimers();
    this.#loseDrive();
    if (this.phase === 'stopped') return;
    if (code === 4401) {
      const token = await this.#deps.getToken(true, this.#authToken).catch(() => null);
      if (token === null) {
        this.#deps.onSessionLost();
        this.stop();
        return;
      }
      this.#connect();
      return;
    }
    if (code === 4429) {
      this.tooMany = true;
      this.#scheduleRetry(TOO_MANY_MIN_MS + backoffDelay(this.#attempt++, this.#deps.random));
      return;
    }
    if (code === 4430 && this.#attempt === 0) {
      this.#attempt += 1;
      this.#scheduleRetry(0);
      return;
    }
    this.#scheduleRetry(backoffDelay(this.#attempt++, this.#deps.random));
  }

  #scheduleRetry(delay: number): void {
    this.phase = 'backoff';
    clearTimeout(this.#retryTimer);
    this.#retryTimer = setTimeout(() => this.#connect(), delay);
  }

  #subscribe(ids: string[]): void {
    for (let i = 0; i < ids.length; i += SUBSCRIBE_CHUNK) {
      this.#send({
        type: 'subscribe',
        mower_ids: ids.slice(i, i + SUBSCRIBE_CHUNK) as [string, ...string[]],
        request_id: this.#requestId(),
      });
    }
  }

  #startPing(): void {
    clearInterval(this.#pingTimer);
    this.#pingTimer = setInterval(() => {
      this.#send({ type: 'ping', request_id: this.#requestId() });
      clearTimeout(this.#pongTimer);
      this.#pongTimer = setTimeout(() => {
        // Half-open socket (phone slept): close on our side and reconnect.
        const ws = this.#ws;
        if (ws) {
          ws.close(4000, 'pong timeout');
          void this.#onClose(ws, 4000);
        }
      }, PONG_TIMEOUT_MS);
    }, PING_INTERVAL_MS);
  }

  #send(message: RealtimeClientMessage): void {
    if (this.#ws && this.#ws.readyState === OPEN) this.#ws.send(JSON.stringify(message));
  }

  #requestId(): string {
    this.#requests += 1;
    return `r${this.#requests}`;
  }

  #clearTimers(): void {
    clearTimeout(this.#retryTimer);
    clearInterval(this.#pingTimer);
    clearTimeout(this.#pongTimer);
  }
}

function clampUnit(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(-1, Math.min(1, value));
}

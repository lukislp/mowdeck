import { describe, expect, it } from 'vitest';

import {
  agoraDebugFields,
  debugLines,
  receiverStats,
  summarizeInbound,
  VideoProbe,
  type DebugInput,
} from './probe';

class FakeTrack extends EventTarget {
  kind = 'video';
  id = 'v1';
  readyState = 'live';
  muted = false;
  enabled = true;
}

describe('VideoProbe', () => {
  it('logs media events of the element and of its tracks', () => {
    let now = 1_000;
    const probe = new VideoProbe(() => now, 4);
    const video = document.createElement('video');
    const track = new FakeTrack();
    Object.defineProperty(video, 'srcObject', {
      value: { getTracks: () => [track] },
      configurable: true,
      writable: true,
    });
    probe.attach(video);
    now = 1_200;
    video.dispatchEvent(new Event('playing'));
    now = 3_400;
    track.dispatchEvent(new Event('mute'));
    video.dispatchEvent(new Event('pause'));
    expect(probe.events.map((e) => `${e.at}:${e.name}`)).toEqual([
      '200:playing',
      '2400:video:mute',
      '2400:pause',
    ]);
    const state = probe.state();
    expect(state?.tracks).toEqual([
      { kind: 'video', id: 'v1', readyState: 'live', muted: false, enabled: true },
    ]);
    // Attaching again does not listen twice; the log keeps the last entries.
    probe.attach(video);
    for (const name of ['waiting', 'stalled', 'emptied']) video.dispatchEvent(new Event(name));
    expect(probe.events).toHaveLength(4);
    expect(probe.events.at(-1)?.name).toBe('emptied');
    probe.detach();
    video.dispatchEvent(new Event('playing'));
    expect(probe.events.at(-1)?.name).toBe('emptied');
    expect(probe.state()).toBeNull();
  });
});

describe('inbound statistics', () => {
  const report = [
    { id: 'c1', type: 'codec', mimeType: 'video/H264', sdpFmtpLine: 'profile-level-id=42e01f' },
    { id: 'a', type: 'inbound-rtp', kind: 'audio', codecId: 'x' },
    {
      id: 'v',
      type: 'inbound-rtp',
      kind: 'video',
      codecId: 'c1',
      framesReceived: 300,
      framesDecoded: 12,
      keyFramesDecoded: 1,
      framesDropped: 0,
      pliCount: 25,
      firCount: 0,
      nackCount: 3,
      decoderImplementation: 'VideoToolbox',
      powerEfficientDecoder: true,
      jitterBufferDelay: 3,
      jitterBufferEmittedCount: 30,
    },
  ];

  it('reads the video entry with its codec', () => {
    const inbound = summarizeInbound(report);
    expect(inbound).toMatchObject({
      codec: 'video/H264 profile-level-id=42e01f',
      decoder: 'VideoToolbox',
      powerEfficient: true,
      framesReceived: 300,
      framesDecoded: 12,
      keyFramesDecoded: 1,
      pliCount: 25,
      jitterMs: 100,
      width: null,
    });
    expect(summarizeInbound([report[1]!])).toBeNull();
  });

  it('reads a receiver and survives one that refuses', async () => {
    const ok = { getStats: async () => new Map(report.map((e) => [e.id, e])) as never };
    expect((await receiverStats(ok))?.framesDecoded).toBe(12);
    const broken = {
      getStats: async () => {
        throw new Error('closed');
      },
    };
    expect(await receiverStats(broken)).toBeNull();
    expect(await receiverStats(null)).toBeNull();
  });

  it('keeps the Agora fields the overlay shows', () => {
    const fields = agoraDebugFields({ codecType: 'H264', decodeFrameRate: 1.234, other: 5 });
    expect(fields.codecType).toBe('H264');
    expect(fields.decodeFrameRate).toBe(1.23);
    expect(fields.receiveFrameRate).toBeNull();
    expect('other' in fields).toBe(false);
  });
});

describe('debugLines', () => {
  it('tells received, decoded and presented frames apart', () => {
    const input: DebugInput = {
      at: '2026-10-17T10:00:00.000Z',
      userAgent: 'Safari',
      phase: 'live',
      frozen: true,
      tiles: [
        {
          slot: 0,
          status: 'live',
          uid: 1,
          element: {
            paused: false,
            readyState: 4,
            networkState: 2,
            width: 1280,
            height: 720,
            currentTime: 12.3,
            presented: 30,
            decoded: 30,
            dropped: 0,
            tracks: [],
          },
          events: [{ at: 200, name: 'resize', detail: '1280x720' }],
          freeze: { intervalMs: 200, thresholdMs: 1_500, sinceMs: 4_000 },
        },
      ],
      report: {
        state: 'CONNECTED',
        codec: 'vp8',
        experiments: 'none',
        tracks: [
          {
            slot: 0,
            uid: 1,
            agora: agoraDebugFields({ codecType: 'H264', receiveFrameRate: 15 }),
            inbound: summarizeInbound([
              { type: 'inbound-rtp', kind: 'video', framesReceived: 300, framesDecoded: 30 },
            ]),
            transform: false,
          },
        ],
      },
    };
    const text = debugLines(input).join('\n');
    expect(text).toContain('frozen=true');
    expect(text).toContain('agora state=CONNECTED codec=vp8 exp=none');
    expect(text).toContain('frames presented=30 decoded=30 dropped=0');
    expect(text).toContain('freeze since=4000ms limit=1500ms interval=200');
    expect(text).toContain('agora H264 -x- fps rx=15');
    expect(text).toContain('rtp frames rx=300 dec=30');
    expect(text).toContain('transform=false');
    expect(text).toContain('200:resize(1280x720)');
  });
});

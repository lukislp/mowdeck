/**
 * Fleet as the front door of the camera relay (ADR 0045 step 5), against the test stack (which
 * always runs camera-relay and MediaMTX with the simulator's `fake` source):
 *
 * - `transport: relay` answers a relay session (WHEP URL per camera, a ticket, a lease);
 * - a WHEP offer from Chromium with the ticket is answered by MediaMTX (201 with an SDP answer),
 *   and the browser receives video frames over the relay's ICE port;
 * - renew answers the same lease with a fresh ticket; delete detaches (204, idempotent), after
 *   which renew is 404; another account sees neither the mower nor the lease (404);
 * - `auto` (no body) uses the relay where it runs; `direct` still answers the vendor session.
 *
 * The web player does not play relay sessions yet (step 6): the app asks for `direct`.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { RUN, actor, createMowerByApi, registerByApi, waitForMap, type Actor } from './support';

interface RelaySession {
  provider: string;
  token: string;
  app_id: string;
  cameras: { id: string }[];
  relay: {
    tiles: { camera_id: string; whep_url: string }[];
    ticket: string;
    lease_id: string;
    heartbeat_seconds: number;
  } | null;
}

function sessionUrl(mowerId: string, suffix = ''): string {
  return `/api/v1/fleet/mowers/${mowerId}/camera/session${suffix}`;
}

async function open(
  request: APIRequestContext,
  who: Actor,
  mowerId: string,
  data?: Record<string, unknown>,
): Promise<{ status: number; body: RelaySession }> {
  const response = await request.post(sessionUrl(mowerId), {
    headers: { Authorization: `Bearer ${who.token}` },
    ...(data ? { data } : {}),
  });
  const text = await response.text();
  return { status: response.status(), body: (text ? JSON.parse(text) : {}) as RelaySession };
}

/**
 * Chromium offers to receive video at `whepUrl` with the ticket as bearer token; returns the
 * HTTP status, and the decoded frame count after a few seconds when the answer was accepted.
 */
async function playWhep(
  page: Page,
  whepUrl: string,
  ticket: string,
): Promise<{ status: number; frames: number; location: string | null }> {
  return page.evaluate(
    async ({ url, bearer }) => {
      const pc = new RTCPeerConnection();
      pc.addTransceiver('video', { direction: 'recvonly' });
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await new Promise<void>((resolve) => {
        if (pc.iceGatheringState === 'complete') resolve();
        pc.addEventListener('icegatheringstatechange', () => {
          if (pc.iceGatheringState === 'complete') resolve();
        });
        setTimeout(resolve, 2_000);
      });
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/sdp', Authorization: `Bearer ${bearer}` },
        body: pc.localDescription?.sdp ?? '',
      });
      const location = response.headers.get('Location');
      if (response.status !== 201) {
        pc.close();
        return { status: response.status, frames: 0, location };
      }
      await pc.setRemoteDescription({ type: 'answer', sdp: await response.text() });
      let frames = 0;
      for (let i = 0; i < 40 && frames < 5; i += 1) {
        await new Promise((resolve) => setTimeout(resolve, 250));
        const stats = await pc.getStats();
        stats.forEach((report: { type: string; kind?: string; framesDecoded?: number }) => {
          if (report.type === 'inbound-rtp' && report.kind === 'video') {
            frames = report.framesDecoded ?? 0;
          }
        });
      }
      pc.close();
      return { status: response.status, frames, location };
    },
    { url: whepUrl, bearer: ticket },
  );
}

test('camera relay: Fleet hands out relay sessions; WHEP plays with the ticket; renew and delete', async ({
  page,
  request,
}) => {
  const owner = await registerByApi(request, actor('camrelay'));
  const stranger = await registerByApi(request, actor('camrelay-other'));
  const mowerId = await createMowerByApi(request, owner, `Luba relay ${RUN}`);
  await waitForMap(request, owner, mowerId);

  // transport relay: the relay's session, unchanged, with every camera of the simulator.
  const opened = await open(request, owner, mowerId, { transport: 'relay' });
  expect(opened.status, JSON.stringify(opened.body)).toBe(200);
  const session = opened.body;
  expect(session.provider).toBe('relay');
  expect(session.token).toBe('');
  expect(session.app_id).toBe('');
  expect(session.relay).not.toBeNull();
  const relay = session.relay!;
  expect(relay.tiles.map((t) => t.camera_id)).toEqual(['front_left', 'front_right']);
  expect(session.cameras.map((c) => c.id)).toEqual(['front_left', 'front_right']);
  for (const tile of relay.tiles) {
    expect(tile.whep_url).toMatch(/^\/media\/whep\/r\/[0-9a-f]{32}\/[a-z_]+\/whep$/);
    expect(tile.whep_url).not.toContain(mowerId);
  }

  // WHEP: without the ticket refused, with it answered, and video frames arrive.
  await page.goto('/');
  const refused = await playWhep(page, relay.tiles[0]!.whep_url, 'v1.not.a-ticket');
  expect(refused.status).toBe(401);
  const played = await playWhep(page, relay.tiles[0]!.whep_url, relay.ticket);
  expect(played.status).toBe(201);
  expect(played.location).toMatch(/^\/r\//);
  expect(played.frames).toBeGreaterThan(0);

  // Renew: the same lease, a fresh ticket.
  const renewUrl = sessionUrl(mowerId, `/${relay.lease_id}/renew`);
  const renewed = await request.post(renewUrl, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  expect(renewed.status(), await renewed.text()).toBe(200);
  expect(renewed.headers()['cache-control']).toBe('no-store');
  const renewedBody = (await renewed.json()) as RelaySession;
  expect(renewedBody.relay?.lease_id).toBe(relay.lease_id);
  expect(renewedBody.relay?.ticket).toBeTruthy();

  // Another account sees neither the mower nor the lease.
  const foreignRenew = await request.post(renewUrl, {
    headers: { Authorization: `Bearer ${stranger.token}` },
  });
  expect(foreignRenew.status()).toBe(404);
  const foreignDelete = await request.delete(sessionUrl(mowerId, `/${relay.lease_id}`), {
    headers: { Authorization: `Bearer ${stranger.token}` },
  });
  expect(foreignDelete.status()).toBe(404);

  // Delete detaches (idempotent); a renew afterwards is 404.
  for (let i = 0; i < 2; i += 1) {
    const deleted = await request.delete(sessionUrl(mowerId, `/${relay.lease_id}`), {
      headers: { Authorization: `Bearer ${owner.token}` },
    });
    expect(deleted.status()).toBe(204);
  }
  const gone = await request.post(renewUrl, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  expect(gone.status()).toBe(404);

  // auto (no body) uses the relay where it runs; direct still answers the vendor session.
  const auto = await open(request, owner, mowerId);
  expect(auto.status).toBe(200);
  expect(auto.body.provider).toBe('relay');
  await request.delete(sessionUrl(mowerId, `/${auto.body.relay!.lease_id}`), {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  const direct = await open(request, owner, mowerId, { transport: 'direct' });
  expect(direct.status).toBe(200);
  expect(direct.body.provider).toBe('fake');
  expect(direct.body.relay).toBeNull();
});

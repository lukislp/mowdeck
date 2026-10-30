# Scripting MowDeck

MowDeck has an HTTP API for everything the web app does. With a **personal access token** your own scripts can read the state of your mowers, start, pause and dock them, read their history and follow them live. This guide shows how, with examples in curl, Python ([httpx](https://www.python-httpx.org/)) and JavaScript (`fetch`, Node 18 or newer and browsers). The full list of operations is in the [API reference](../reference/api/index.md); the design is in [ADR 0064](../adr/0064-personal-access-tokens-and-public-api-docs.md).

## Try it live on the demo

A public demo installation ([ADR 0068](../adr/0068-public-demo-mode.md)) has an interactive **API explorer**: open the demo, click **Try the demo**, then **Try the API live** in the demo band (or go to `/api-explorer` of the demo, for example `http://localhost:8580/api-explorer` for a demo you run yourself, see [Public demo](../ops/demo.md)). It shows every service's operations from the same OpenAPI documents as the [API reference](../reference/api/index.md) and sends "Test Request" calls with your demo session, so you need no token there. The mowers are simulated, actions the demo does not allow answer 403 `demo-mode` (creating tokens among them), and the demo resets every few hours. On your own installation the explorer is off; use the steps below. Design: [ADR 0072](../adr/0072-api-explorer-in-the-web-app.md).

## 1. Create a personal access token

In the web app open **Einstellungen → API-Zugang** (Settings → API access):

1. Give the token a name that says what it is for ("Rain script", "Garden display").
2. Choose what it may do:

    | Scope | Allows |
    | --- | --- |
    | `mower.view` (Lesen) | See the mowers, their state, telemetry, history, map and live data. Required with every other scope. |
    | `mower.control` (Steuern) | Start, pause and resume mowing, send mowers to the dock (and the emergency stop, which anyone who sees a mower may send). |
    | `schedule.edit` (Zeitpläne) | Create, change, skip and delete schedules. |

    A personal token can **never** drive a mower by hand, change devices or firmware, or manage users, roles, tokens or billing, and it never holds more than you do: a viewer gets a read-only token.
3. Choose how long it lives: 30, 90, 180 days or one year. Every personal token expires; create a new one before then.
4. Confirm with your passkey or password (MowDeck asks for it before it creates a credential, see [Confirming critical actions](step-up.md)).

The token (`mdk_...`) is shown **once**. MowDeck stores only a SHA-256 hash of its secret; if you lose it, revoke it and create a new one. The list shows each token's scopes, when it expires and when it was last used. **Widerrufen** (Revoke) stops it at once. Admins of the account see personal tokens in the account's API token list as well and can revoke them too. You can hold up to 25 active personal tokens.

Keep the token out of your scripts: put it into an environment variable or your secret store.

```sh
export MOWDECK_URL=https://mowdeck.example.org/api/v1   # your installation + /api/v1
export MOWDECK_TOKEN=mdk_...                             # never commit this
```

## 2. Authenticate: exchange the token for an access token

The personal token is not sent with every request. Exchange it for an **access token** that is valid for 10 minutes, then send that as a Bearer token:

```sh
curl -s -X POST "$MOWDECK_URL/identity/token" \
  -H 'Content-Type: application/json' \
  -d "{\"grant_type\": \"api_token\", \"token\": \"$MOWDECK_TOKEN\"}"
```

```json
{
  "access_token": "eyJhbGciOiJFZERTQSIs...",
  "token_type": "Bearer",
  "expires_in": 600,
  "expires_at": "2026-10-28T12:10:00Z",
  "account_id": "6f1c..."
}
```

Use the access token until shortly before `expires_at` (or until a request answers 401), then exchange again. Do not exchange for every request: the exchange is rate limited (below).

## 3. Base URL, rate limits and errors

**Base URL.** The API shares the origin with the web app: `https://<your installation>/api/v1/<service>`, for example `/api/v1/fleet/mowers`. The services are `identity`, `fleet`, `telemetry`, `scheduler`, `notifications`, `planner`, `vision`, `entitlements`, `billing`, `realtime` and `device-gateway`.

**Rate limits.** The token exchange (`POST /identity/token`) shares the login limit of the gateway: 10 per minute per client address with a burst of 20, plus Identity's own limit per address. Over the limit the answer is **429** with `Retry-After` (seconds). Reusing the access token for its 10 minutes keeps a script far below it. The other endpoints have no gateway limit today; poll at a sensible rate (a state every few seconds at most) or use the [realtime WebSocket](#realtime-websocket) instead of polling.

**Errors** are [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem documents (`Content-Type: application/problem+json`):

```json
{
  "type": "urn:mowdeck:problem:forbidden",
  "title": "Forbidden",
  "status": 403,
  "detail": "forbidden",
  "instance": "/api/v1/fleet/mowers/9a5c.../commands",
  "correlation_id": "4f0e2b0c-..."
}
```

| Status | `type` (after `urn:mowdeck:problem:`) | Meaning |
| --- | --- | --- |
| 400 | `bad-request` | The request makes no sense as sent |
| 401 | `unauthorized` | Missing, invalid or expired access token, or a revoked or expired personal token at the exchange: exchange again or create a new token |
| 403 | `forbidden` | The token's scopes do not allow this (a mower you see, but may not control) |
| 404 | `not-found` | Does not exist, or not visible to the token (indistinguishable on purpose) |
| 409 | `conflict` | The state does not allow it right now |
| 422 | `validation-failed` | The body or a parameter is invalid; `errors` lists each field with `pointer` or `parameter` |
| 429 | `too-many-requests` | Rate limited; wait `Retry-After` seconds |
| 503 | `unavailable` | A dependency is down; retry with backoff |

Every response carries `X-Correlation-ID`; quote it (or the problem's `correlation_id`) when you report a problem. You can send your own `X-Correlation-ID` to find your request in the logs.

## 4. Examples

The examples read `MOWDECK_URL` and `MOWDECK_TOKEN` from the environment.

### A small client

=== "Python (httpx)"

    ```python
    import os
    import time

    import httpx

    BASE = os.environ["MOWDECK_URL"]
    TOKEN = os.environ["MOWDECK_TOKEN"]


    class MowDeck:
        """Exchanges the personal token and renews the access token before it runs out."""

        def __init__(self) -> None:
            self.http = httpx.Client(base_url=BASE, timeout=15)
            self.access, self.until = "", 0.0

        def _auth(self) -> dict[str, str]:
            if time.time() > self.until - 60:
                answer = self.http.post(
                    "/identity/token", json={"grant_type": "api_token", "token": TOKEN}
                )
                answer.raise_for_status()
                body = answer.json()
                self.access, self.until = body["access_token"], time.time() + body["expires_in"]
            return {"Authorization": f"Bearer {self.access}"}

        def get(self, path: str, **params: object) -> dict:
            answer = self.http.get(path, params=params, headers=self._auth())
            answer.raise_for_status()
            return answer.json()

        def post(self, path: str, body: dict) -> dict:
            answer = self.http.post(path, json=body, headers=self._auth())
            answer.raise_for_status()
            return answer.json()


    md = MowDeck()
    ```

=== "JavaScript (fetch)"

    ```js
    const BASE = process.env.MOWDECK_URL;
    const TOKEN = process.env.MOWDECK_TOKEN;

    let access = '';
    let until = 0;

    async function auth() {
      if (Date.now() > until - 60_000) {
        const res = await fetch(`${BASE}/identity/token`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ grant_type: 'api_token', token: TOKEN }),
        });
        if (!res.ok) throw new Error(`exchange failed: ${res.status} ${await res.text()}`);
        const body = await res.json();
        access = body.access_token;
        until = Date.now() + body.expires_in * 1000;
      }
      return { Authorization: `Bearer ${access}` };
    }

    async function api(path, { method = 'GET', body } = {}) {
      const res = await fetch(`${BASE}${path}`, {
        method,
        headers: { ...(await auth()), ...(body ? { 'Content-Type': 'application/json' } : {}) },
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!res.ok) throw new Error(`${method} ${path}: ${res.status} ${await res.text()}`);
      return res.status === 204 ? null : res.json();
    }
    ```

=== "curl"

    ```sh
    ACCESS=$(curl -s -X POST "$MOWDECK_URL/identity/token" \
      -H 'Content-Type: application/json' \
      -d "{\"grant_type\": \"api_token\", \"token\": \"$MOWDECK_TOKEN\"}" | jq -r .access_token)
    AUTH="Authorization: Bearer $ACCESS"
    ```

The tabs above render on the documentation site; on GitHub and in the wiki the three variants simply follow each other.

### List the mowers

`GET /fleet/mowers` answers `{"items": [...]}` with every mower the token may see ([reference](../reference/api/fleet.md)).

```sh
curl -s "$MOWDECK_URL/fleet/mowers" -H "$AUTH" | jq '.items[] | {id, name, model}'
```

```python
for mower in md.get("/fleet/mowers")["items"]:
    print(mower["id"], mower["name"], mower["model"])
```

```js
const { items } = await api('/fleet/mowers');
for (const mower of items) console.log(mower.id, mower.name, mower.model);
```

### Get the current state

`GET /telemetry/mowers/{id}/state` answers the newest state: `state.activity` (`idle`, `mowing`, `paused`, `returning`, `docked`, `charging`, `error`, `offline`, `updating`, `unknown`), `state.battery_percent`, `state.job` (progress, area, remaining time), `state.position`, `state.gnss` and more ([reference](../reference/api/telemetry.md)). `stale` is true when the mower has not reported for a while. All mowers at once: `GET /telemetry/accounts/me/mowers/states`.

```sh
MOWER=9a5c...   # an id from the list
curl -s "$MOWDECK_URL/telemetry/mowers/$MOWER/state" -H "$AUTH" \
  | jq '{stale, activity: .state.activity, battery: .state.battery_percent, job: .state.job}'
```

```python
snapshot = md.get(f"/telemetry/mowers/{mower_id}/state")
state = snapshot["state"]
print(state["activity"], state["battery_percent"], "stale" if snapshot["stale"] else "")
```

```js
const { state, stale } = await api(`/telemetry/mowers/${mowerId}/state`);
console.log(state.activity, state.battery_percent, stale ? '(stale)' : '');
```

### Start, pause, dock

Commands need the `mower.control` scope. `POST /fleet/mowers/{id}/commands` with `{"command": {"kind": ...}}` answers **202** with a `command_id`; the mower's answer follows. Read it with `GET /fleet/commands/{command_id}` (`status`: `pending`, `accepted`, `executed`, `rejected`, `expired`, `failed`) or watch `command_result` on the WebSocket.

`start_mow` needs the areas to mow (or a stored plan): the area ids are in the mower's map, `GET /telemetry/mowers/{id}/map` (`map.areas[].id`, `name`).

```sh
# Start the first area of the map
AREA=$(curl -s "$MOWDECK_URL/telemetry/mowers/$MOWER/map" -H "$AUTH" | jq -r '.map.areas[0].id')
curl -s -X POST "$MOWDECK_URL/fleet/mowers/$MOWER/commands" -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d "{\"command\": {\"kind\": \"start_mow\", \"area_ids\": [\"$AREA\"]}}"

# Pause, resume, back to the dock (end_job: true ends the job instead of resuming it later)
curl -s -X POST "$MOWDECK_URL/fleet/mowers/$MOWER/commands" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"command": {"kind": "pause_mow"}}'
curl -s -X POST "$MOWDECK_URL/fleet/mowers/$MOWER/commands" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"command": {"kind": "resume_mow"}}'
curl -s -X POST "$MOWDECK_URL/fleet/mowers/$MOWER/commands" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"command": {"kind": "return_to_dock", "end_job": true}}'
```

```python
import time

areas = md.get(f"/telemetry/mowers/{mower_id}/map")["map"]["areas"]
accepted = md.post(
    f"/fleet/mowers/{mower_id}/commands",
    {"command": {"kind": "start_mow", "area_ids": [areas[0]["id"]]}},
)
for _ in range(30):  # wait up to a minute for the mower's answer
    record = md.get(f"/fleet/commands/{accepted['command_id']}")
    if record["status"] not in ("pending", "accepted"):
        break
    time.sleep(2)
print(record["status"], record.get("reason"))

md.post(f"/fleet/mowers/{mower_id}/commands", {"command": {"kind": "pause_mow"}})
md.post(
    f"/fleet/mowers/{mower_id}/commands",
    {"command": {"kind": "return_to_dock", "end_job": True}},
)
```

```js
const { map } = await api(`/telemetry/mowers/${mowerId}/map`);
const accepted = await api(`/fleet/mowers/${mowerId}/commands`, {
  method: 'POST',
  body: { command: { kind: 'start_mow', area_ids: [map.areas[0].id] } },
});
console.log('accepted', accepted.command_id);

await api(`/fleet/mowers/${mowerId}/commands`, {
  method: 'POST',
  body: { command: { kind: 'return_to_dock', end_job: true } },
});
```

A rain sensor script, for example, pauses every mowing mower:

```python
for mower in md.get("/fleet/mowers")["items"]:
    if md.get(f"/telemetry/mowers/{mower['id']}/state")["state"]["activity"] == "mowing":
        md.post(f"/fleet/mowers/{mower['id']}/commands", {"command": {"kind": "pause_mow"}})
```

### Read today's history

`GET /telemetry/mowers/{id}/history?from=...&to=...&step=...` answers the state over time: without `step` the raw rows, with `step` (`5s`, `1m`, `1h`, `1d`) one point per bin with the last activity, the battery mean, minimum and maximum, charging and the last position ([reference](../reference/api/telemetry.md)). Timestamps are ISO 8601; give the time zone.

```sh
FROM=$(date -u +%Y-%m-%dT00:00:00Z)
curl -s -G "$MOWDECK_URL/telemetry/mowers/$MOWER/history" -H "$AUTH" \
  --data-urlencode "from=$FROM" --data-urlencode "step=15m" \
  | jq -r '.points[] | [.at, .activity, .battery_percent] | @tsv'
```

```python
from datetime import datetime, time as day_time
from zoneinfo import ZoneInfo

midnight = datetime.combine(
    datetime.now(ZoneInfo("Europe/Berlin")).date(), day_time(0), tzinfo=ZoneInfo("Europe/Berlin")
)
history = md.get(
    f"/telemetry/mowers/{mower_id}/history", **{"from": midnight.isoformat(), "step": "15m"}
)
mowing = [p for p in history["points"] if p["activity"] == "mowing"]
print(
    f"{len(mowing) * 15} minutes mowing today, battery now {history['points'][-1]['battery_percent']} %"
)
```

```js
const midnight = new Date();
midnight.setHours(0, 0, 0, 0);
const query = new URLSearchParams({ from: midnight.toISOString(), step: '15m' });
const { points } = await api(`/telemetry/mowers/${mowerId}/history?${query}`);
console.table(points.map((p) => ({ at: p.at, activity: p.activity, battery: p.battery_percent })));
```

Health totals per day are at `GET /telemetry/mowers/{id}/health/daily`, the account's report at `GET /telemetry/reports/summary`.

### Realtime WebSocket

Instead of polling, open one WebSocket at `wss://<installation>/api/v1/realtime/ws` (`ws://` without TLS). Personal tokens work here as well: the first message is `auth` with an **access token** (not the `mdk_` token), then `subscribe` names the mowers. The server pushes `state` for every new state and `command_result` for every command status change. Send `auth` again with a fresh access token before `token_expires_at` from `ready`, or the socket closes with 4401. The protocol is in [ADR 0018](../adr/0018-realtime-websocket-protocol.md) and the [realtime README](https://github.com/lukislp/mowdeck/blob/main/services/realtime/README.md).

=== "Python (websockets)"

    ```python
    import asyncio
    import json

    import websockets  # pip install websockets


    async def follow(mower_id: str) -> None:
        url = BASE.replace("https://", "wss://").replace("http://", "ws://") + "/realtime/ws"
        async with websockets.connect(url) as ws:

            async def authenticate() -> None:
                md.until = 0  # force a fresh exchange
                md._auth()
                await ws.send(json.dumps({"type": "auth", "token": md.access}))

            async def renew() -> None:
                while True:  # a new access token every 9 minutes, same socket
                    await asyncio.sleep(540)
                    await authenticate()

            await authenticate()
            renewing = asyncio.create_task(renew())  # keep a reference while the socket lives
            subscribed = False
            async for raw in ws:
                message = json.loads(raw)
                if message["type"] == "ready" and not subscribed:  # ready follows every auth
                    await ws.send(json.dumps({"type": "subscribe", "mower_ids": [mower_id]}))
                    subscribed = True
                elif message["type"] == "state":
                    state = message["state"]
                    print(state["activity"], state["battery_percent"])
                elif message["type"] == "command_result":
                    print("command", message["result"]["status"])


    asyncio.run(follow("9a5c..."))
    ```

=== "JavaScript (WebSocket)"

    ```js
    // Node 22+ and browsers have WebSocket built in.
    const ws = new WebSocket(`${BASE.replace(/^http/, 'ws')}/realtime/ws`);
    ws.onopen = async () => {
      ws.send(JSON.stringify({ type: 'auth', token: (await auth()).Authorization.slice(7) }));
    };
    ws.onmessage = async (event) => {
      const message = JSON.parse(event.data);
      if (message.type === 'ready') {
        ws.send(JSON.stringify({ type: 'subscribe', mower_ids: [mowerId] }));
        // Renew a minute before the access token runs out.
        const renewIn = Date.parse(message.token_expires_at) - Date.now() - 60_000;
        setTimeout(async () => {
          until = 0; // force a new exchange
          ws.send(JSON.stringify({ type: 'auth', token: (await auth()).Authorization.slice(7) }));
        }, renewIn);
      } else if (message.type === 'state') {
        console.log(message.state.activity, message.state.battery_percent);
      } else if (message.type === 'command_result') {
        console.log('command', message.result.status);
      }
    };
    ws.onclose = (event) => console.log('closed', event.code); // reconnect with backoff
    ```

Reconnect with backoff after the close codes 1001, 1006 and 4430, and after 4401 with a new access token.

## 5. Manage tokens from a script

Personal tokens are managed with a **user session**, never with a token: `GET`, `POST /identity/me/api-tokens` and `DELETE /identity/me/api-tokens/{id}` answer 403 to any API token, so a leaked token cannot mint or hide others. Creating one also needs a recent step-up. If a token leaks, revoke it in the settings; it stops working at the next exchange, and an access token already issued with it runs out within 10 minutes.

## See also

- [API reference](../reference/api/index.md), generated from the OpenAPI documents of this version.
- The API explorer of a demo installation (`/api-explorer`, [ADR 0072](../adr/0072-api-explorer-in-the-web-app.md)): the same documents, interactive.
- [Home Assistant integration](../dev/home-assistant.md): the same API, with an account API token created by an admin.
- [Confirming critical actions](step-up.md).

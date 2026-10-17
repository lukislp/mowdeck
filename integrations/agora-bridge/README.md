# agora-bridge

The optional Agora source of MowDeck's camera relay ([ADR 0045](../../docs/adr/0045-camera-relay.md), step 7). It joins a mower's Agora channel with Agora's Linux server SDK as a receive-only member and hands the encoded camera frames to `camera-relay` over the [camera source bridge protocol](../../docs/dev/camera-relay-bridge-protocol.md). Without it the relay answers Agora mowers (Mammotion) with `unsupported_provider`, and the web app keeps using the direct path of ADR 0032.

Licence of this directory: **Apache-2.0** (`LICENSE`), unlike the rest of `integrations/` (AGPL-3.0). The Agora SDK is **not** part of it, see [Licence](#licence).

## What is in here

| Path | Content |
| --- | --- |
| `src/agora_bridge/protocol.py` | The protocol: control lines (stdin) and records (stdout); standard library only |
| `src/agora_bridge/bridge.py`, `outbox.py` | One bridge process: control handling, SDK callbacks to records, back-pressure (bounded queue, delta frames dropped until the next key frame) and the exit codes |
| `src/agora_bridge/agora_sdk.py` | The adapter over `agora-python-server-sdk` (receive-only audience, `encodedFrameOnly`, encryption, renew) |
| `src/agora_bridge/guard.py` | The safe import of the wrapper: no download, TLS verification restored (see [Security](#security)) |
| `src/agora_bridge/server.py` | The TCP server next to the relay: one bridge process per connection |
| `docker/install_sdk.py`, `Dockerfile` | The image build: pinned downloads with SHA-256, no `pip install` of the wrapper |
| `tests/` | Protocol, control handling, back-pressure, exit codes, the guard and the server, with a fake SDK (no network, no SDK) |
| `spike/` | The step 0 spike against the real mower and its recorded stream (`spike/fixtures/luba-uid1.h265`, the relay's test fixture) |

## How the relay talks to it

The relay image never contains the SDK, so the relay cannot start the bridge as its own child process. The bridge runs in its own container that **shares the relay's network namespace** (Compose `network_mode: service:camera-relay`; Helm: a second container of the relay's pod) and listens on loopback only (`127.0.0.1:7700`):

```text
camera-relay --tcp://127.0.0.1:7700--> agora-bridge server --stdin--> python -m agora_bridge (one per session) --SDK--> Agora
             <--- frame/event records ---                  <--stdout--
```

- One TCP connection is one bridge process, one channel join. The server starts the process at the first control line (a connection that closes before is the relay's reachability probe) and copies bytes verbatim in both directions; the bridge's stderr goes to the server's log with a session number.
- When the process ends, the server appends `{"event": "exit", "code": N}` and closes the connection. Closing the connection closes the bridge's stdin: it leaves the channel and exits (killed 10 s later otherwise).
- At most `AGORA_BRIDGE_MAX_SESSIONS` (default 4) processes; another connection gets `error` `busy` and `exit` 1.
- Before it listens, the server runs `python -m agora_bridge --check`; when the SDK does not load (offline) or TLS verification is off, it exits with 3 and never listens, so the relay keeps answering `unsupported_provider`.
- Vendor tokens and keys travel only on the bridge's stdin (inside the relay's own network namespace), never in arguments, environment, files or logs.

Settings of the server (environment): `AGORA_BRIDGE_LISTEN` (`127.0.0.1:7700`), `AGORA_BRIDGE_MAX_SESSIONS` (4), `AGORA_BRIDGE_LOG_LEVEL` (`INFO`).

## Build the image

Building it downloads Agora's SDK and **means accepting Agora's SDK licence terms**. Build it on the machine that runs MowDeck; never push it to a public registry. No MowDeck CI job builds or publishes it.

```bash
docker build --platform linux/amd64 -t mowdeck-local/agora-bridge:local integrations/agora-bridge
```

The build:

1. downloads `agora-python-server-sdk` 2.4.9 (MIT, the sdist, 94,571 bytes, SHA-256 `3f9453ce4730c1b9db86e56ea9b96713ffc576060b357d9bf0de8f65b473031e`) and the native SDK `agora_rtc_sdk_x86_64-linux-gnu-v4.4.32.174_28020_SERVER_20260601_1240_1144448_20251021_1427-3a.zip` (33,340,805 bytes, SHA-256 `66f8a30dc75bb4f50a0597b52445c2b197bbc1970cd632262a24cdffe39b9130`) from their pinned URLs over verified TLS and fails on any other size or digest;
2. copies only the wrapper's `agora/` package (its `setup.py` never runs, `pip` is not used) and the archive's `agora_sdk/` directory to `/opt/agora-sdk/agora/`, and writes `agora/version.txt` with exactly the archive URL the wrapper expects;
3. checks that the native library finds all its dependencies (`ldd`);
4. runs `python -m agora_bridge --check` **without network access** (`RUN --network=none`): the SDK loads, nothing is downloaded, TLS verification is intact.

The image is x86_64 only (the pinned native SDK), runs as uid 10001 with a read-only root file system and writes the SDK's log to a private directory under `/tmp` (a tmpfs), removed when the session ends.

With Compose the stack builds it for you (profile `agora`, `pull_policy: build`), see [Compose operations](../../docs/ops/compose.md#agora-source-profile-agora). For Kubernetes push it to a registry you control and set `services.cameraRelay.agoraBridge` ([Helm operations](../../docs/ops/helm.md#agora-source)).

## Security

The pip package `agora-python-server-sdk` does two things the bridge does not accept, and the build and the bridge counter both:

- **It downloads the native SDK without a hash check**, at install time (`setup.py`) and again at import time (`agora/__init__.py`, after an `rm -rf` of its library directory) whenever `agora/version.txt` does not name its expected archive. The image never runs `setup.py`, downloads the archive itself with a pinned SHA-256 and writes `version.txt`; `agora_bridge.guard.check_installation` refuses to import when the import would download, and the import runs with `urllib.request.urlretrieve` and `urlopen` blocked.
- **It turns off TLS certificate verification for the whole process** (`ssl._create_default_https_context = ssl._create_unverified_context` in three modules). `agora_bridge.guard.import_agora` restores the previous context after the import and checks that it is the standard library's `ssl.create_default_context`; the bridge refuses to start otherwise (exit 3). The bridge makes no HTTPS calls of its own anyway; the native SDK's own connections are not affected by Python's setting.

Further:

- The wrapper raises a harmless `TypeError` in `_on_capabilities_changed` when the connection observer's `on_aiqos_capability_missing` returns None; the bridge's observer returns -1 (keep the audio scenario).
- stdout carries only protocol records: the bridge points file descriptor 1 at stderr before it loads the SDK, because the wrapper and the native SDK print.
- The wrapper's two log lines at `ERROR` that only name its paths on every import are filtered; everything else of it is logged from `WARNING` on. The bridge logs no token, key, salt, app id or channel.

## Licence

- The code here is Apache-2.0. It loads the SDK at run time only and links nothing of MowDeck's AGPL services; the relay talks to it over the documented protocol.
- `agora-python-server-sdk` is MIT. The native Agora SDK is proprietary and ships no licence file; it is governed by Agora's terms. **MowDeck never redistributes it**: no published image contains it, and the image is built by the operator, who accepts Agora's terms by building it.
- The bridge joins with the app id and the short-lived tokens the vendor issues for the mower's owner, the same session the browser uses on the direct path (ADR 0032).

## Tests

```bash
uv run pytest integrations/agora-bridge/tests
```

The tests run without the SDK and without network: the protocol's byte layout, every control op and its errors (no secret in an error), the exit codes (0, 2, 4, 1), back-pressure (a full queue drops delta frames until the next key frame and reports the count), the adapter against a stand-in for the wrapper's modules, the guard (an installation that would download is refused, TLS verification is restored after an import that disables it) and the server (bytes both ways, the exit record, probes start nothing, the session limit, a vanished relay, the check; the token never in a child's arguments or environment).

The relay's side is tested in `services/camera-relay`: unit tests with a fake bridge server, and the media integration tests, which run this server from source with a stand-in bridge that replays `spike/fixtures/luba-uid1.h265` through the protocol, so the H.265 to H.264 path is checked end to end in CI without Agora.

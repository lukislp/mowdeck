# Camera source bridge protocol

A source bridge is a separate process that receives a mower's camera from a vendor's media cloud with that vendor's SDK and hands the encoded frames to `camera-relay` ([ADR 0045](../adr/0045-camera-relay.md)). There is one bridge process per source session (per channel join), and it talks to the relay over its standard streams only; when the bridge runs in a container of its own, a [socket transport](#socket-transport) carries the same bytes. The protocol is vendor neutral: `agora-bridge` is the first implementation, and any other SDK can be wrapped the same way. Version 1 is described here.

## Streams

| Stream | Direction | Content |
| --- | --- | --- |
| stdin | relay → bridge | Control lines: one JSON object per line, UTF-8, terminated by `\n` |
| stdout | bridge → relay | Binary records: frames and events |
| stderr | bridge → relay | Plain log lines for the relay's log, never a secret |

**Secrets travel only on stdin**: never in arguments, environment variables, files or logs. The bridge holds them in memory only and writes no file. Closing stdin means `leave`: the bridge leaves the channel and exits.

## Control lines (stdin)

Every line has an `op`. Unknown fields are ignored; an unknown `op` is answered with an `error` event and otherwise ignored.

| `op` | Fields | Meaning |
| --- | --- | --- |
| `join` | `app_id`, `channel`, `token`, `uid`, `encryption` (`{mode, key, salt}` or null), optional `profile` | Join as a receive-only member with the session's credentials. Sent once, first. |
| `renew` | `token`, optional `uid` | Renew the token on the running connection. When the bridge cannot (the SDK refuses, or the uid differs and the SDK needs a rejoin), it reports `renew_failed` and the relay joins again with the fresh session. |
| `subscribe` | `uids` (list) | Subscribe to these publishers' encoded video. Publishers that join later are subscribed when listed. |
| `keyframe` | `uid` | Ask the publisher for a key frame. Best effort: some publishers ignore it. |
| `leave` | | Leave the channel, flush, exit with code 0. |

Example (values shortened):

```json
{"op": "join", "app_id": "…", "channel": "…", "token": "…", "uid": 123456, "encryption": null}
{"op": "subscribe", "uids": [1, 2, 3]}
```

## Records (stdout)

Every record is a 4-byte big-endian length `L` followed by `L` bytes: one type byte and the body. `L` is at most 4 MiB + 64; a longer record is a protocol error and the relay stops the bridge.

### Frame (type `0x01`)

| Field | Size | Meaning |
| --- | --- | --- |
| `uid` | u32 BE | Publisher uid (one camera) |
| `codec` | u8 | `1` H.264, `2` H.265 (Annex-B) |
| `flags` | u8 | bit 0 `keyframe`, bit 1 `capture_ts` present |
| `recv_mono_ns` | u64 BE | When the bridge received the frame, on its monotonic clock |
| `capture_ts` | u64 BE | Sender's capture time in ms, if bit 1 is set (else 0) |
| `bytes` | rest | One access unit in Annex-B; a key frame carries its parameter sets |

`capture_ts` is the sender's own clock, not wall time (on the LUBA a monotonic clock in 100 ms steps): it is informational and never used for latency. The relay takes presentation timestamps from `recv_mono_ns` (the true intervals between frames as they arrived), continued monotonically across bridge processes.

### Event (type `0x02`)

A UTF-8 JSON object with an `event` field:

| `event` | Fields | Meaning |
| --- | --- | --- |
| `hello` | `protocol` (1), `bridge`, `sdk` | First record after start |
| `joined` | `elapsed_ms` | The connection is up |
| `publisher` | `uid`, `state` (`published`, `unpublished`) | A camera started or stopped publishing |
| `video_state` | `uid`, `state` (`starting`, `decoding`, `frozen`, `stopped`), optional `dropped` | The SDK's view of a camera's stream and frames dropped since the last report, for metrics |
| `token_will_expire` | | The SDK warns that the token runs out soon |
| `renewed` / `renew_failed` | `reason` (on failure) | Outcome of a `renew` |
| `error` | `code`, `detail` | A failure; fatal ones are followed by exit |
| `left` | | The channel was left; the bridge exits next |

Events never carry a token, key or salt.

## Behaviour

- The bridge never blocks the SDK's callback thread: it copies each buffer into a bounded queue. When the queue is full it drops delta frames until the next key frame of that uid and reports the count in the next `video_state` event (`dropped`).
- It sends nothing it did not receive: no repeated or synthetic frames.
- Exit codes: `0` after `leave` or closed stdin, `2` bad input, `3` SDK failed to load, `4` join failed, `1` anything else. The relay treats any exit outside `leave` as a source failure (state `FAILED`, retry with backoff).
- The bridge makes no network calls besides the SDK's own.

## Socket transport

The relay image never contains a vendor SDK, so a bridge built as an image of its own (`agora-bridge`) is reached over TCP: a small server in the bridge's container listens on the relay's loopback (the containers share a network namespace) and starts one bridge process per connection.

- The connection carries exactly the bytes above: what the relay writes is the bridge's stdin, what the bridge writes to stdout comes back. stderr stays in the server's log.
- The server starts the bridge at the first control line; a connection that closes before is a reachability probe and starts nothing (the relay answers `unsupported_provider` while nothing listens).
- When the bridge process ends, the server sends one more event record, `{"event": "exit", "code": N}` with the exit code above, and closes the connection. A server that runs too many bridges answers `error` (`code` `busy`) and `exit` 1.
- The relay closing the connection closes the bridge's stdin, which means `leave`.

## The relay's driver (Agora)

How `camera-relay` uses a bridge (`services/camera-relay`, `adapters/media/bridge_source.py`):

- `join` with the vendor session, then `subscribe` with the uids of the leased cameras; a camera whose uid never publishes never gets media and blocks nothing.
- Per camera, from its first key frame on, the access units go into an MPEG transport stream with presentation times from `recv_mono_ns` and are published to the media server as H.265 (`r/<rid>/<cam>/src`); the generic transcoder makes the H.264 viewer stream. Nothing is repeated or invented; a muxer that falls behind drops delta frames until the next key frame.
- Renewal about five minutes before the session expires: a fresh session from the gateway; the same channel gets `renew` on the running connection, otherwise (or on `renew_failed`, no answer within 10 s, or `token_will_expire` afterwards) a second connection joins with the fresh session, each camera switches to it at its first key frame, and the old connection gets `leave`.
- The active connection ending without `leave` (an `exit` record, a closed connection, a protocol error) fails the source: the session goes `FAILED` with its backoff.

The relay's tests run against a fake bridge that replays a recorded stream through this protocol, so CI needs no vendor SDK.

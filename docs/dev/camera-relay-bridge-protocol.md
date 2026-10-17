# Camera source bridge protocol

A source bridge is a separate process that receives a mower's camera from a vendor's media cloud with that vendor's SDK and hands the encoded frames to `camera-relay` ([ADR 0045](../adr/0045-camera-relay.md)). The relay starts one bridge per source session and talks to it over the bridge's standard streams only. The protocol is vendor neutral: `agora-bridge` is the first implementation, and any other SDK can be wrapped the same way. Version 1 is described here.

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
| `renew` | `token`, optional `uid` | Renew the token on the running connection. When the bridge cannot (the SDK refuses, or the uid differs and the SDK needs a rejoin), it reports `renew_failed` and the relay sends `leave` and a new `join`. |
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

`capture_ts` is the sender's own clock, not wall time: the relay rebases it to the first frame and uses it for presentation timestamps, never for latency. Without it the relay uses `recv_mono_ns`.

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

The relay's tests run against a fake bridge that replays a recorded stream through this protocol, so CI needs no vendor SDK.

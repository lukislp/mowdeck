# Internal TLS

Every connection inside a MowDeck installation is encrypted, not only the edge. The browser and the apps reach the gateway over the operator's TLS (proxy, Tailscale or ingress, see [Compose, HTTPS](compose.md)); from there on, every hop between two containers uses TLS with a certificate of the installation's own private CA, and every client verifies the certificate of the other side, including its name. Nothing in the repository turns that verification off.

It is on by default and needs no setting: the Compose stack creates the CA and the certificates itself on the first start.

## What is encrypted

| Hop | Protocol | Server certificate | The client verifies with |
| --- | --- | --- | --- |
| Traefik to every service and the web app | HTTPS | the service's (`identity`, `fleet`, ..., `web`) | one servers transport per service in `infra/compose/traefik/internal-tls.yml` (CA and service name) |
| Service to service (JWKS, module descriptors, Billing to Entitlements, Device-Gateway to camera-relay, Identity's demo sandboxes, the assistant) | HTTPS | the called service's | `SSL_CERT_FILE` (the CA plus the public roots) |
| Services to NATS | NATS over TLS (`tls://nats:4222`) | `nats` | `MOWDECK_TLS_CA_FILE` |
| Services and OpenFGA to PostgreSQL | PostgreSQL with TLS (`hostssl` only, `pg_hba.conf`) | `postgres` | `MOWDECK_TLS_CA_FILE` (asyncpg), `sslmode=verify-full` (OpenFGA) |
| Services and `authz-bootstrap` to OpenFGA | HTTPS (HTTP and gRPC API) | `openfga` | `SSL_CERT_FILE` |
| camera-relay to MediaMTX's API, MediaMTX's auth callback to camera-relay | HTTPS | `mediamtx`, `camera-relay` | `SSL_CERT_FILE` |
| Traefik to MediaMTX's WHEP server | HTTPS | `mediamtx` | servers transport `mediamtx` |
| Traefik and Identity to the Matter server (host network) | HTTPS | `matter-server` (also named `host.docker.internal`) | servers transport `matter-server`, `SSL_CERT_FILE` |

The audio and video of the live camera travel over WebRTC, which encrypts them itself (DTLS-SRTP).

## What stays unencrypted

| Hop | Why |
| --- | --- |
| A container's own loopback: health checks, the NATS monitoring endpoint (`127.0.0.1:8222`), OpenFGA's metrics, the web app's plain HTTP listener (`127.0.0.1:8080`), PostgreSQL's socket and loopback (init scripts, `pg_dumpall`, `psql` in the container) | Never leaves the container. |
| camera-relay to the agora-bridge (`tcp://127.0.0.1:7700`, profile `agora`) | The bridge shares camera-relay's network namespace and listens on its loopback only. |
| The Matter server to Traefik (`MATTER_API_URL`, default `http://127.0.0.1:<GATEWAY_HTTP_PORT>`) | The Matter server runs on the host network and calls Traefik on the host's own loopback. |
| camera-relay to MediaMTX over RTSP (`rtsp://mediamtx:8554`, profile `relay`) | ffmpeg's RTSP client cannot check an RTSPS server certificate (it offers no CA option for `rtsps://`), so RTSPS would encrypt without authenticating the server. The publisher authenticates with a per-process secret, the port is reachable inside the stack's network only, and the hop carries the camera picture of the relay profile only. Moving MediaMTX into camera-relay's network namespace (loopback) is the planned way to close it. |
| Kubelet probes in Kubernetes | The kubelet does not verify probe certificates; probes carry no data. |

The external services are the operator's own choice and use whatever their URL says: AI providers, the weather service, push services and the vendor clouds are reached over HTTPS; an `http://` URL you configure yourself (for example a local Ollama, `VISION_DEFAULT_API_BASE=http://ollama:11434`) stays HTTP.

## Certificates in Compose

The one-shot service `tls-init` runs before every other container on each `docker compose up` (and `./deploy.sh`):

- On the first start it creates the private CA (EC P-256, valid 20 years) in the volume `tls-ca`, which only `tls-init` mounts.
- It issues one certificate per server (valid 5 years) into the volume `tls`, named after the service plus `localhost`, `127.0.0.1` and `::1` (the health checks), and hands each key (mode 0400) to the user of the container that reads it. Every container mounts `tls` read-only.
- It writes `ca.crt` (the CA) and `ca-bundle.pem` (the public roots plus the CA). Containers point `SSL_CERT_FILE` at the bundle, so calls to the internet keep working.
- Every later run keeps what is still valid and issues a certificate again when it is missing, does not match its key, the CA or the names, or expires within a year. A CA within five years of its end is replaced; the previous one stays trusted until it expires, so containers that were not recreated yet keep working.

Containers load their certificate at start, so a renewed certificate is used once its container is recreated, which every deploy does. To force it: `docker compose up -d --force-recreate`.

`docker compose down -v` deletes both volumes; the next start creates a new CA.

### Tools on the host

The host ports stay bound to `127.0.0.1` and now speak TLS:

```bash
# PostgreSQL: psql's default sslmode=prefer works as is.
psql -h 127.0.0.1 -U postgres
# The CA, for clients that verify (nats CLI, curl against OpenFGA):
docker compose cp tls-init:/etc/mowdeck/tls/ca.crt ./mowdeck-ca.crt   # works while tls-init exists
nats --tlsca ./mowdeck-ca.crt -s tls://127.0.0.1:4222 --user <user> --password <password> server check connection
curl --cacert ./mowdeck-ca.crt --resolve openfga:8080:127.0.0.1 https://openfga:8080/healthz
# NATS monitoring is inside the container only:
docker compose exec nats wget -qO- http://127.0.0.1:8222/varz
```

`docker compose exec -T postgres pg_dumpall -U postgres` (the backup of `deploy.sh`) uses the socket and needs nothing.

## Upgrading an existing installation

Nothing to do: `./deploy.sh <version>` (or `docker compose pull && docker compose up -d --wait`) starts `tls-init`, which creates the CA and the certificates, and every container comes up with TLS. No new key in `.env` is required.

An `.env` from an earlier release may still name an internal service with `http://`, for example `OPENFGA_API_URL=http://openfga:8080` (in every `.env` copied from the old example), `GATEWAY_RELAY_URL=http://camera-relay:8000` or `IDENTITY_EXTRA_MODULE_URLS=http://assistant:8000`. With internal TLS those services speak HTTPS only, so such a URL is used as `https://` automatically (the service logs `internal http:// URLs used as https://` with the setting's name; `authz-bootstrap` logs the OpenFGA URL it uses). Change the value to `https://` when convenient; nothing breaks in the meantime. This applies to the host names of the stack's own services (`identity`, `fleet`, ..., `openfga`, `mediamtx`, `host.docker.internal` for the Matter server) and never to any other host.

The public demo (`demo/reset.sh`, `docker-compose.demo.yml`) starts from fresh volumes on each reset and gets a new CA every time; its `demo.env` needs no new key either.

## Settings

| Variable | Where | Meaning |
| --- | --- | --- |
| `MOWDECK_TLS_CERT_FILE`, `MOWDECK_TLS_KEY_FILE` | Python services | Certificate and key of the service's HTTPS listener; both empty: plain HTTP (tests, development outside the stack) |
| `MOWDECK_TLS_CA_FILE` | Python services, `authz-bootstrap` | The CA for NATS and PostgreSQL; set, it also turns `http://<service>` URL settings into `https://` |
| `SSL_CERT_FILE` | every container with an HTTP client | The trust store of the HTTP clients (Python, Go): the public roots plus the CA |
| `MOWDECK_WEB_TLS_CERT_FILE`, `MOWDECK_WEB_TLS_KEY_FILE` | web | HTTPS on 8443; plain HTTP on 8080 then listens on the loopback only |
| `MOWDECK_MATTER_TLS_CERT_FILE`, `MOWDECK_MATTER_TLS_KEY_FILE` | matter-server | HTTPS for the admin API (`NODE_EXTRA_CA_CERTS` names the CA for its health check) |

These are set by `docker-compose.yml`; `.env` needs none of them. Running a single service outside the stack (tests, `uv run python -m <service>.main`) without them keeps plain HTTP, as before.

## Debugging

Every container has the CA at `/etc/mowdeck/tls/ca.crt`, so a request from inside the network works with verification:

```bash
docker compose exec identity python -c "import urllib.request; print(urllib.request.urlopen('https://fleet:8000/readyz').read())"
openssl s_client -connect 127.0.0.1:5432 -starttls postgres -CAfile ./mowdeck-ca.crt -verify_hostname postgres
```

A handshake that fails names the reason in the client's log (`CERTIFICATE_VERIFY_FAILED`, `hostname mismatch`). The usual cause is a certificate that a container loaded before a renewal: recreate the containers (`docker compose up -d --force-recreate`).

## Kubernetes

The Helm chart's own internal TLS (cert-manager or a CA of the chart) is described in [Helm](helm.md).

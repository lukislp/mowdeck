# Matter server

The Matter server puts every mower into Apple Home, Google Home or Home Assistant as its own Matter device (shown as a robot vacuum: Matter has no mower type yet): start, pause, return to the dock, state and battery. There is no emergency stop over Matter. Design and decisions: [ADR 0063](../adr/0063-matter-server.md). User guide: [Apple Home and Google Home](../guides/matter.md).

It is optional and off by default: Compose profile `matter`, Helm `services.matterServer.enabled`.

## Requirements

- **Same network as the controller.** Matter finds devices with mDNS and talks IPv6 link-local. The MowDeck host and the Matter controller (Apple TV or HomePod as home hub, a Google Nest hub, or Home Assistant) must be on the same layer-2 network (VLAN), or you must bridge mDNS (an mDNS reflector) and route IPv6 between them. The iPhone pairing a device must be on that network too.
- **IPv6 on the host.** Link-local addresses are enough (`ip -6 addr` shows an `fe80::` address on the LAN interface); do not disable IPv6 in the kernel.
- **Host networking.** The container runs with `network_mode: host` (Kubernetes: `hostNetwork: true`). It cannot work on Docker's bridge network.
- One **API token per account** whose mowers should appear.

## Ports

| Port | Protocol | From | Purpose |
| --- | --- | --- | --- |
| 5353 | UDP | LAN (IPv4 multicast 224.0.0.251, IPv6 `ff02::fb`) | mDNS: the mowers announce themselves |
| `MATTER_PORT_BASE` ... `+MATTER_MAX_NODES-1` (5540-5555) | UDP | LAN, IPv4 and IPv6 link-local | One Matter node per mower |
| `MATTER_HTTP_PORT` (8790) | TCP | Docker's bridge networks only | Admin API (pairing codes) for Traefik and Identity's module descriptor |

The admin API checks every request with Identity, but it has no business on the LAN: Traefik serves it at `/api/v1/matter`.

## Setup with Compose (MS-01, Debian)

1. **API token.** Sign in as an account admin, **Einstellungen › API-Tokens › Token erstellen**: name `Matter-Server`, **Berechtigungen: Home Assistant – Grundfunktionen** (exactly `mower.view` and `mower.control` on the whole account), nothing more. Copy the token (it is shown once).
2. **Token file** (one token per line, one line per account; `#` starts a comment):

   ```sh
   cd infra/compose
   install -d -m 0750 secrets/matter
   (umask 077 && $EDITOR secrets/matter/api-tokens)   # paste the token
   sudo chown 1000:1000 secrets/matter/api-tokens && sudo chmod 0400 secrets/matter/api-tokens
   ```

   The directory is git-ignored. The token never appears in a log.
3. **`.env`:**

   ```sh
   COMPOSE_PROFILES=matter            # add to existing profiles: relay,matter
   IDENTITY_EXTRA_MODULE_URLS=http://host.docker.internal:8790
   # optional: MATTER_MDNS_INTERFACE=enp2s0, MATTER_LANGUAGE=en, MATTER_PORT_BASE=5540
   ```

   `MATTER_API_URL` defaults to Traefik on the host (`http://127.0.0.1:<GATEWAY_HTTP_PORT>`); set it only when the API is elsewhere.
4. **Firewall** (below), then start: `docker compose up -d --wait` (with a release: the usual `deploy.sh`; the image is signed and verified like the others).
5. **Check:** `docker compose logs matter-server` shows `matter.node.started` once per mower (with its port, never a code). In the web app, **Einstellungen › Matter** lists the mowers; pairing: [guide](../guides/matter.md).

## Firewall (nftables)

Allow mDNS and the node ports from the LAN only (here `192.168.0.0/16` and IPv6 link-local), and the admin port from Docker's bridges only. Merge into your `inet filter` table's input chain (it must have `policy drop` or end with a drop for this to matter):

```nft
table inet filter {
  chain input {
    # ... existing rules (established/related, loopback, ssh) ...

    # Matter (MowDeck matter-server, ADR 0063)
    ip  saddr 192.168.0.0/16 udp dport 5353 accept comment "mDNS from the LAN"
    ip6 saddr fe80::/10      udp dport 5353 accept comment "mDNS (IPv6) from the LAN"
    ip  saddr 192.168.0.0/16 udp dport 5540-5555 accept comment "Matter nodes"
    ip6 saddr fe80::/10      udp dport 5540-5555 accept comment "Matter nodes (IPv6)"
    ip  saddr 172.16.0.0/12  tcp dport 8790 accept comment "Matter admin API from Docker bridges"
    tcp dport 8790 drop comment "Matter admin API: nobody else"
  }
}
```

If your LAN uses IPv6 unique-local or global addresses for the hub, add their prefix next to `fe80::/10`. Docker publishes no port of this service (host network), so Docker's own nftables rules do not open anything here. Reload with `sudo nft -f /etc/nftables.conf` and check with `sudo nft list ruleset`.

Avahi on the host can stay: matter.js shares UDP 5353.

## Helm

```yaml
services:
  matterServer:
    enabled: true
    nodeSelector: {kubernetes.io/hostname: ms01}   # a node in the hub's LAN
secrets:
  MATTER_API_TOKENS: "mdk_..."                      # --set-file or SOPS, one per line
config:
  MATTER_API_URL: ""                               # default https://<ingress.host>
```

The pod uses `hostNetwork` with the `Recreate` strategy and a 64 MiB PersistentVolumeClaim (`persistence`). The Ingress routes `/api/v1/matter` to it and Identity lists its descriptor. Open the same ports on the node as above.

## Operations

- **Backups:** the `matter-data` volume (`nodes.json` with the setup codes, `matter/` with the fabrics). Without it every mower must be paired again everywhere. It contains secrets: back it up like the `.env`.
- **New mower:** appears within a minute.
- **Removed mower:** its node is factory-reset after two minutes without it in a complete answer; remove the tile in the Home app.
- **Revoking access:** revoke the API token (**Einstellungen › API-Tokens**): the server can no longer read or command that account's mowers (the tiles show an error). Reset a mower's pairing in **Einstellungen › Matter** to throw every controller out.
- **More than 16 mowers:** raise `MATTER_MAX_NODES` and widen the firewall range.
- **Logs:** JSON lines; `matter.command` per controller command (intent and plan, no code). Pairing codes and passcodes are never logged.

## Without the Matter server: Home Assistant

Installations that already run Home Assistant with the MowDeck integration can expose its `lawn_mower` entity through [Home Assistant Matter Hub](https://github.com/RiDDiX/home-assistant-matter-hub) instead (it maps `lawn_mower` to the same robot vacuum type; use its server mode for Apple Home). The MowDeck Matter server needs no Home Assistant.

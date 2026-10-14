# 3. Context and scope

## Business context

Users operate their mowers through the web app, Home Assistant or other API clients. The platform talks to mowers through the Device-Gateway only, using Bluetooth bridges or (later) WLAN.

## System context (C4 level 1)

```mermaid
flowchart LR
    user["User / sub-user"]
    ha["Home Assistant (HACS integration)"]
    ext["Other API clients"]
    operator["Platform operator"]
    mower["Mammotion mower"]
    bridge["ESP32 bridge"]
    weather["Weather service (Open-Meteo)"]
    pay["Payment provider (Stripe), optional"]
    push["Push / ntfy / e-mail"]

    subgraph mowdeck["MowDeck platform"]
        core["Services, event bus, databases"]
    end

    user -->|"web app (PWA)"| core
    ha -->|"REST + WebSocket, API token"| core
    ext -->|"public API /api/v1"| core
    operator -->|"operator console"| core
    bridge -->|"outbound TLS, device secret"| core
    bridge <-->|"Bluetooth"| mower
    core -->|"future: WLAN via PyMammotion"| mower
    core --> weather
    core --> pay
    core --> push
```

## Technical context

| Interface | Technology | Direction |
| --- | --- | --- |
| Public API | REST under `/api/v1` and WebSocket, through Traefik | clients to platform |
| Bridge link | outbound TLS connection with a device secret | bridge to Device-Gateway |
| Mower | Bluetooth (bridge) or WLAN, via PyMammotion | Device-Gateway to mower |
| Internal bus | NATS JetStream | between services |
| Payment | Stripe adapter (Checkout, customer portal, signed webhooks) | Billing to provider |
| Notifications | Web Push, ntfy, e-mail | Notifications to channels |
| Weather | Open-Meteo adapter | Scheduler to provider |

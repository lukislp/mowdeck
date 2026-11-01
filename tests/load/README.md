# Load test

k6 scenarios and a runner that starts a throwaway Compose stack shaped like the public demo's
host (two CPU cores, about 7 GB) and measures it. How to run it, the results and the
recommendations are in [docs/ops/load-testing.md](../../docs/ops/load-testing.md).

Never point it at the public demo, a home installation or any other real server: the runner only
talks to the stack it starts itself, inside that stack's own Docker network.

| File | Content |
| --- | --- |
| `load.mjs` | the runner: `up`, `down`, `k6 <scenario>`, `sweep`, `run`, `stats` (Node, no dependencies) |
| `report.mjs`, `report.test.mjs` | docker stats and k6 summaries to Markdown tables (`node --test tests/load`) |
| `docker-compose.load.yml` | the stack override: CPU pinning, memory limits, the fixed subnet |
| `load.env` | the stack's settings on top of `infra/compose/.env.example` (test values) |
| `lib/mowdeck.js` | shared k6 helpers: visitors, the web app's requests, the live view |
| `scenarios/demo-visitor.js` | (a) demo visitors with the live view |
| `scenarios/rest-mix.js` | (b) signed-in users clicking through the app |
| `scenarios/command-burst.js` | (c) start, pause, resume, dock at a rising rate |
| `scenarios/rate-limits.js` | (d) the per-address limits, measured on their own |

```bash
node tests/load/load.mjs up                 # build and start mowdeck-load at 127.0.0.1:8590
node tests/load/load.mjs k6 demo-visitor    # one scenario; results in tests/load/results/
node tests/load/load.mjs sweep              # demo visitors at 25, 50, 100, ... until it breaks
node tests/load/load.mjs down               # stop it and delete its volumes
```

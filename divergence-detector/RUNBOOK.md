# Running the Divergence Detector

End-to-end runbook. Assumes you have **Docker Desktop** installed and running
(macOS / Windows / Linux). Total time: ~5 minutes start to finish, most of it
the seed script.

## 1 · Prerequisites

Check Docker is installed and the daemon is running:

```bash
docker --version
docker compose version
```

You should see both commands print a version. If Docker isn't installed, grab
it from <https://docs.docker.com/get-docker/>.

## 2 · Get the code

Unpack the tarball into a working directory:

```bash
tar -xzf divergence-api.tar.gz
cd divergence-api
```

The folder should look like:

```
divergence-api/
├── app/                  # FastAPI app
├── web/index.html        # Wired UI
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── README.md
```

## 3 · (Optional) Set your Anthropic API key

If you want real Claude-generated narratives, export your key before bringing
the stack up. Without it, the API runs in mock mode and returns templated
narratives — everything else still works.

```bash
export ANTHROPIC_API_KEY=sk-ant-your-key-here
```

If you don't have a key, also export this so the API skips the SDK call entirely:

```bash
export USE_MOCK_LLM=true
```

On Windows PowerShell, use `$env:ANTHROPIC_API_KEY = "sk-ant-..."` instead.

## 4 · Bring up the stack

```bash
docker compose up -d
```

This pulls two images and starts two containers:

- `divergence-db` — Postgres 16 with TimescaleDB and pgvector
- `divergence-api` — the FastAPI app

First time this runs it'll download a few hundred MB of images. Subsequent
boots take ~5 seconds.

Verify both containers are healthy:

```bash
docker compose ps
```

Both should show `running` and the db should show `(healthy)`. If the api
container is restarting, check logs with `docker compose logs api`.

Test the API is reachable:

```bash
curl http://localhost:8000/health
# {"status":"ok","version":"0.7.2"}
```

## 5 · Seed the database

This generates ~2 years of synthetic implied + realized metrics across six
instruments, scores the full history (so pgvector has neighbors to search),
labels three historically significant events, and creates a demo user with
three subscriptions.

```bash
docker compose exec api python -m app.seed
```

You'll see progress logs. Expect ~2-4 minutes total. The final line should
read `✓ seed complete`.

## 6 · Open the UI

The UI is a single HTML file — no build step, no node. Just open it:

**macOS:**
```bash
open web/index.html
```

**Linux:**
```bash
xdg-open web/index.html
```

**Windows:**
```bash
start web/index.html
```

Or double-click `web/index.html` in your file manager.

The UI auto-detects whether the API is reachable. In the top-right corner
you'll see either:

- `LIVE API` (cyan badge) — connected, pulling real data from your local stack
- `DEMO · MOCK DATA` (orange badge) — API not reachable, falling back to
  hardcoded demo data

If you see `DEMO`, the API isn't on `localhost:8000`. Check `docker compose ps`.

## 7 · Verify the wiring

**Dashboard.** The page-load headline should be generated from the actual
divergence data ("Execution is dislocating in FNCL_30Y…" or similar). Cards
show the latest scored event per instrument. Click any card → detail view
fetches event + narrative + analogs from three parallel API calls.

**Narrative.** On the detail page, the AI narrative is loaded from the API.
If you set `ANTHROPIC_API_KEY`, this is real Claude output. Click `↻
Regenerate` to force a fresh call (replaces the cached narrative).

**Analogs.** The historical analog list is the live result of a pgvector
cosine search excluding events within 30 days. Labels come from the
`event_labels` table seeded during step 5.

**Subscriptions.** Three demo subs visible. Click any toggle → API call
to `PATCH /subscriptions/{id}`. Refresh the page to confirm the new state
persisted.

**Live alerts.** The WebSocket auto-connects on page load. The scoring
worker runs every 15 minutes (configurable via `SCORING_INTERVAL_MINUTES`);
when it finds a `|z| >= 2.0` event matching one of your subscriptions, a
toast pops in the bottom-right.

To trigger an alert immediately without waiting for the worker, run the
scoring tick manually:

```bash
docker compose exec api python -c "
import asyncio
from app.workers import scoring_tick
asyncio.run(scoring_tick())
"
```

This synthesizes a new event at the current timestamp; the highest-|z|
instruments should produce visible toasts.

## 8 · Things to try

```bash
# List all instruments
curl http://localhost:8000/instruments

# Current divergences
curl http://localhost:8000/divergence/current | jq

# Detail for event 1
curl http://localhost:8000/divergence/1 | jq
curl -X POST http://localhost:8000/divergence/1/narrative | jq
curl http://localhost:8000/divergence/1/analogs | jq

# Subscriptions (needs the seeded API key)
curl -H "X-API-Key: demo-api-key-change-me" \
     http://localhost:8000/subscriptions/ | jq

# Ingest a commentary snippet
curl -X POST http://localhost:8000/commentary/ \
  -H "Content-Type: application/json" \
  -d '{"raw_text": "MBS basis wider; conventional 30y screens illiquid above benchmark size.", "source": "GS_MBS"}' | jq

# Interactive Swagger UI
open http://localhost:8000/docs
```

## 9 · Common issues

**`docker compose up` fails with "image not found".** The
`timescale/timescaledb-ha:pg16` tag is the right one as of 2026, but
Timescale occasionally renames their tags. If pull fails, edit
`docker-compose.yml` and try `timescale/timescaledb:latest-pg16` instead,
then `CREATE EXTENSION vector;` manually after first boot.

**API container restarts in a loop.** Check `docker compose logs api`. Most
common cause is the db not being ready when the api boots; the healthcheck
should prevent this but if it's racing, restart with
`docker compose restart api`.

**Seed script errors out partway.** It's idempotent — re-run
`docker compose exec api python -m app.seed`. Existing rows are skipped via
`ON CONFLICT DO NOTHING`.

**UI shows `DEMO · MOCK DATA` even though the API is up.** Likely a CORS
issue if you're opening the HTML from a non-`localhost` origin. The API
allows `*` by default; if you're hosting the UI elsewhere, confirm
`CORS_ORIGINS` in `docker-compose.yml` matches. You can also point the UI
at a different API by setting `window.DIVERGENCE_API` before the script runs.

**Narratives all look templated even with my API key set.** Check that the
key got into the container: `docker compose exec api env | grep ANTHROPIC`.
If empty, the container was started before you exported the variable —
`docker compose down && docker compose up -d` after exporting will pick it up.

**Wipe and start fresh:**

```bash
docker compose down -v   # -v removes the db volume
docker compose up -d
docker compose exec api python -m app.seed
```

## 10 · Tear down

```bash
docker compose down       # stop containers, keep data
docker compose down -v    # stop + delete the db volume
```

---

**That's it.** Five commands for the happy path:

```bash
tar -xzf divergence-api.tar.gz && cd divergence-api
export ANTHROPIC_API_KEY=sk-ant-...     # optional
docker compose up -d
docker compose exec api python -m app.seed
open web/index.html
```

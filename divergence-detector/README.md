# Divergence Detector

> AI-powered intelligence for U.S. institutional rates markets. Detects when markets are pricing more stress than execution validates — and the more dangerous inverse: when execution is dislocated while implied signals stay calm.

<p>
  <img alt="Python" src="https://img.shields.io/badge/python-3.12-blue.svg">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.115-009688.svg">
  <img alt="PostgreSQL" src="https://img.shields.io/badge/PostgreSQL-16-4169E1.svg">
  <img alt="TimescaleDB" src="https://img.shields.io/badge/TimescaleDB-pg16-FDB515.svg">
  <img alt="pgvector" src="https://img.shields.io/badge/pgvector-0.3-336791.svg">
  <img alt="Claude" src="https://img.shields.io/badge/Claude-Sonnet%204.5-D97757.svg">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green.svg">
</p>

## What it does

Traditional liquidity measures — bid-ask spreads, implied volatility, dealer commentary — tell you what markets are *pricing*. They don't tell you what they're *executing*. The gap between those two signals matters most during stress: during Liberation Day in April 2025, the VIX moved above 65, but actual transaction cost data told a far more measured story than during March 2020.

This tool puts both signals in the same frame:

- **Implied stress composite** — option-implied vol (MOVE, VIX), dealer sentiment, option skew
- **Realized stress composite** — T-Cost, intraday vol, composite pricing width
- **Divergence score** — implied z-score minus realized z-score

When the divergence crosses ±2σ, the system writes an event, generates an AI-authored narrative (Claude Sonnet 4.5), finds historical analogs via pgvector cosine search, and routes alerts to subscribed channels.

## Architecture

```
┌─────────────────┐         ┌──────────────────────────────────────┐
│   Web UI        │         │              FastAPI                 │
│   (HTML/JS)     │◄────────┤  /divergence  /analogs  /commentary  │
│                 │   REST  │  /subscriptions  /ws/live            │
└─────────────────┘   WS    └──────────────────────────────────────┘
                                            │
                              ┌─────────────┼─────────────┐
                              │             │             │
                       ┌──────▼─────┐ ┌─────▼─────┐ ┌────▼────────┐
                       │  Scoring   │ │ Narrative │ │  Dispatch   │
                       │  (z-score) │ │  (Claude) │ │  (subs+WS)  │
                       └──────┬─────┘ └─────┬─────┘ └────┬────────┘
                              │             │             │
                              └─────────────┼─────────────┘
                                            │
                          ┌─────────────────▼─────────────────┐
                          │   PostgreSQL 16 + TimescaleDB     │
                          │   + pgvector                      │
                          │                                   │
                          │   • Hypertables: time-series      │
                          │   • ivfflat index: analog search  │
                          └───────────────────────────────────┘
```

## Quick start

```bash
git clone https://github.com/<you>/divergence-detector.git
cd divergence-detector

cp .env.example .env
# Edit .env to add your ANTHROPIC_API_KEY (optional; uses mock fallback if blank)

docker compose up -d
docker compose exec api python -m app.seed   # ~3 min — generates 2 years of synth data

open web/index.html
```

Detailed steps and troubleshooting are in [`RUNBOOK.md`](./RUNBOOK.md).

## Stack

- **FastAPI** — async HTTP + WebSocket
- **PostgreSQL 16** with **TimescaleDB** (hypertables) and **pgvector** (analog search)
- **SQLAlchemy 2.0** async ORM
- **APScheduler** — in-process scoring loop
- **Anthropic Messages API** (Claude Sonnet 4.5) for narrative generation, with deterministic mock fallback for offline development
- **Vanilla HTML/JS UI** — no build step, auto-detects API availability with graceful fallback

## Project structure

```
divergence-detector/
├── app/
│   ├── main.py              # FastAPI app + lifespan
│   ├── config.py            # Settings (Pydantic)
│   ├── db.py                # Async engine + session
│   ├── models.py            # SQLAlchemy ORM
│   ├── schemas.py           # Pydantic request/response
│   ├── init_db.sql          # Extensions, hypertables, indexes
│   ├── seed.py              # Synthetic data generator
│   ├── workers.py           # APScheduler scoring loop
│   ├── routers/
│   │   ├── divergence.py    # /divergence/* — events, narrative, analogs
│   │   ├── subscriptions.py # /subscriptions/* — CRUD + delivery log
│   │   ├── commentary.py    # /commentary — ingestion + classification
│   │   ├── reference.py     # /instruments, /analogs/search
│   │   └── ws.py            # /ws/live — live alert stream
│   └── services/
│       ├── scoring.py       # z-score math + feature vector encoding
│       ├── analog_finder.py # pgvector cosine NN
│       ├── narrative.py     # Anthropic API + prompt + mock fallback
│       ├── dispatch.py      # subscription match + cooldown + delivery
│       └── ws_manager.py    # in-process WebSocket registry
├── web/
│   └── index.html           # Wired UI (auto-detects API, falls back to mock)
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── RUNBOOK.md               # Detailed setup + troubleshooting
├── .env.example
├── .gitignore
└── LICENSE
```

## API surface

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/health` | Liveness probe |
| `GET`  | `/instruments` | List tracked instruments |
| `GET`  | `/divergence/current` | Latest event per instrument |
| `GET`  | `/divergence/series` | Time series for one instrument or aggregate |
| `GET`  | `/divergence/{id}` | Single event |
| `POST` | `/divergence/{id}/narrative` | Get or generate AI narrative |
| `GET`  | `/divergence/{id}/analogs` | K nearest historical events |
| `GET`  | `/analogs/search` | Find analogs by date (standalone finder) |
| `POST` | `/commentary/` | Ingest + classify + embed dealer chat |
| `*`    | `/subscriptions/*` | Subscription CRUD + delivery log |
| `WS`   | `/ws/live?api_key=...` | Live alert stream |

Full schema at `http://localhost:8000/docs` once running.

## How scoring works

Every 15 minutes (configurable), the worker:

1. Loads the trailing 252 days of implied and realized metrics per instrument from the hypertables.
2. Computes per-component rolling z-scores; takes the mean for an implied composite and a realized composite.
3. `divergence_score = implied_z − realized_z`.
4. Classifies regime: `calm`, `stressed`, `dislocated` (|divergence| > 2.5).
5. Builds a 32-d feature vector capturing the day's cross-asset shape — per-asset-class z-scores, dispersion, sign patterns. This is what pgvector searches over for analog lookup.
6. Upserts into `divergence_events`.
7. For events with `|divergence_score| >= 2.0`, runs `dispatch_event` to find matching subscriptions, check cooldowns, generate the narrative once, and route to all subscriber channels.

The feature vector is *handcrafted* rather than learned: 32 dimensions, every dim with semantic meaning. This keeps the index tiny (~128 bytes per row) and makes neighbor explanations interpretable — see commentary in `app/services/scoring.py` for the layout, and the `pgvector` discussion below.

## Design notes

**TimescaleDB hypertables** auto-partition `implied_metrics` and `realized_metrics` by time. Queries with a `ts` filter hit only relevant chunks, so the scoring loop stays fast even with years of data.

**pgvector ivfflat** with cosine distance gives sublinear analog search. The 32-d engineered feature vector trades the recall of learned embeddings for full interpretability — when the system says "today resembles SVB weekend," you can decompose the vector and see exactly which dims drove the match.

**Narrative caching** keys on `(divergence_event_id, prompt_version)`. Re-rendering a narrative without bumping `prompt_version` is free. The `prompt_version` constant in `config.py` is the lever for A/B testing prompt changes.

**Cooldown** is per-subscription per-instrument, enforced by a query against `alert_deliveries` rather than in-memory state — survives worker restarts and scales horizontally.

**`UNIQUE (subscription_id, divergence_event_id)`** on deliveries is the de-dup guarantee. Even if the scorer reruns or a job replays, a user cannot receive the same alert twice.

## What's intentionally not built

This is a research-grade prototype, not a production system. Notable gaps:

- **Auth** is a static API key per user. Real deployment needs JWT/OAuth.
- **Email** delivery is logged but not actually sent (placeholder for SES/Postmark/Resend).
- **WebSocket manager** is in-process; for multi-node deployment, route through Redis pub/sub.
- **Worker** runs in-process via APScheduler. At higher throughput, move to Celery beat with a separate worker pool.
- **Metric weights** table exists in the schema but isn't yet read by the scorer — wired for future per-asset-class weighting.

## Inspiration

The framing borrows from the Tradeweb Liquidity Cost Index: measuring liquidity in basis points actually traded rather than in implied snapshots. The asymmetry insight — that execution stressed with implied calm is historically the more dangerous regime, not the inverse — is what gives the AI narrative its sharpest take.

## License

MIT — see [`LICENSE`](./LICENSE).

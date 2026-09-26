<!-- TODO(author): replace with your real Loom/voice-over link before submitting -->
[![Loom walkthrough](https://img.shields.io/badge/Loom-5--min%20walkthrough-blueviolet)](https://www.loom.com/share/REPLACE_WITH_YOUR_REAL_ID)

# AI/LLM QA API

Production-ready FastAPI service that authenticates users (JWT + RBAC), calls
an LLM through a gateway with timeouts, retries, fallback and a concurrency
cap, caches answers and rate-limits in Redis, persists users and chat logs
(tokens, latency, status) in PostgreSQL, and exposes Prometheus metrics.

| Doc | What's in it |
|-----|--------------|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Components, auth/SSO/OIDC path, RBAC, trade-offs |
| [`SCALING.md`](SCALING.md) | 100 → 500 RPS plan (§4 of the brief) and single-EC2 → K8s migration (§5) |
| [`docs/architecture.svg`](docs/architecture.svg) | Architecture diagram |
| [`k8s/README.md`](k8s/README.md) | Kubernetes deployment |
| [`terraform/README.md`](terraform/README.md) | AWS foundation (EKS, RDS, ElastiCache) |

![Architecture](docs/architecture.svg)

---

## Stack

| Layer | Technology |
|-------|------------|
| API | FastAPI + Uvicorn |
| Auth | JWT (HS256, `iss`/`aud` validated) via PyJWT, bcrypt password hashing |
| LLM | Any OpenAI-compatible provider (OpenAI, Azure, Groq, Together, vLLM, Ollama) |
| Cache / rate limit | Redis (answer cache; sliding-window limiter as atomic Lua) |
| Database | PostgreSQL 16 (SQLAlchemy 2) |
| Observability | Prometheus metrics, structured JSON logs, `X-Request-ID` |
| Container | Multi-stage Docker image, non-root |
| Orchestration | Docker Compose (single + 3-replica behind NGINX), Kubernetes (Kustomize, HPA, PDB) |
| Tests | pytest (67 tests), mocked LLM (httpx MockTransport), fakeredis, in-memory SQLite |

---

## Endpoints

| Method | Path | Roles | Purpose |
|--------|------|-------|---------|
| POST | `/auth/login` | public | Issue JWT |
| GET | `/auth/me` | any authenticated | Caller profile |
| POST | `/chat` | admin, user | Ask the LLM (rate limited) |
| GET | `/chat/history` | any authenticated | Caller's own recent Q&A |
| GET | `/reports/usage` | admin, read_only | Aggregate requests / tokens / latency by model |
| GET, POST | `/admin/users` | admin | List / create users |
| PATCH | `/admin/users/{id}` | admin | Change role, enable/disable |
| GET | `/health` | public | Deep health: `ok` / `degraded` (Redis down) / `down` (DB down → 503) |
| GET | `/health/live` | public | Liveness probe (no dependency checks) |
| GET | `/health/ready` | public | Readiness probe (DB required; Redis reported, non-fatal) |
| GET | `/metrics` | admin JWT or `METRICS_TOKEN` | Prometheus metrics |

OpenAPI docs: http://localhost:8000/docs — click **Authorize** and paste the
`access_token` from `/auth/login`.

### `/chat` behaviour

| Situation | Response |
|-----------|----------|
| Success | `200` with `answer`, `model`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `latency_ms`, `fallback_used`, `cached` |
| Missing / invalid / expired token | `401` |
| `read_only` role | `403` |
| Empty or blank question, >4000 chars | `422` |
| Per-user or global rate limit | `429` + `Retry-After`, `X-RateLimit-*` |
| LLM error after retries + fallback | `502` + `Retry-After` |
| Provider rate limiting us / local concurrency cap reached | `503` + `Retry-After` |
| LLM timeout (per call or total 45 s budget) | `504` + `Retry-After` |

Every request records latency (`api_request_latency_seconds`, response header
`X-Response-Time-ms`, `chat_logs.latency_ms`) and every LLM call records
token usage (`llm_tokens_total{model,type}`, `chat_logs.prompt_tokens/completion_tokens`).

---

## Web interface

http://localhost:8000/ui/ serves a small dependency-free chat console (JWT
login, chat, cache/latency/fallback indicators, history).

---

## Quick start (Docker Compose)

```bash
git clone https://github.com/kmdsuhail72/ai-qa-api.git
cd ai-qa-api
cp .env.example .env
# Edit .env:
#   JWT_SECRET=$(openssl rand -hex 32)
#   LLM_API_KEY=<your provider key>
#   POSTGRES_PASSWORD=<anything>
#   ADMIN_PASSWORD / DEMO_USER_PASSWORD / DEMO_READONLY_PASSWORD=<your choice>
#
# Free local option (Ollama running on the host):
#   LLM_BASE_URL=http://host.docker.internal:11434/v1
#   LLM_API_KEY=ollama
#   LLM_MODEL=llama3.2:1b
#   LLM_FALLBACK_MODEL=

docker compose --env-file .env -f docker/docker-compose.yml up --build
```

API: http://localhost:8000. Tables are created and bootstrap users seeded on
startup (only users whose password env var is set).

> Run compose **from the repo root with `--env-file .env`**: Compose reads
> variables for `${...}` interpolation from the compose file's directory by
> default, not from the repo root.

### Three replicas behind NGINX (load-balancing demo)

```bash
docker compose --env-file .env -f docker/docker-compose.scale.yml up --build
# NGINX on http://localhost:33085 (least_conn, per-IP limit, X-Upstream header shows the replica)
```

### Demo flow

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$ADMIN_PASSWORD\"}" | jq -r .access_token)

curl -s -X POST localhost:8000/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"Explain Docker in one sentence."}' | jq

# Same question again -> served from Redis ("cached": true, ~ms latency)
curl -s -X POST localhost:8000/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"explain docker in one sentence."}' | jq

curl -s localhost:8000/health | jq
curl -s localhost:8000/reports/usage -H "Authorization: Bearer $TOKEN" | jq
curl -s localhost:8000/metrics -H "Authorization: Bearer $TOKEN" | grep -E '^(llm_|chat_|api_request)'
```

---

## Local development without Docker

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
# needs Postgres + Redis reachable per .env (DATABASE_URL, REDIS_URL)
uvicorn app.main:app --reload
```

## Testing

```bash
pytest -v                                  # 67 tests, ~3 s, no external services
pytest --cov=app --cov-report=term-missing
ruff check app tests
```

Tests use in-memory SQLite, fakeredis and a mocked LLM (gateway fakes plus
`httpx.MockTransport` for the real OpenAI-compatible client). They cover:
auth/JWT, the RBAC matrix, admin user lifecycle, usage reports, cache, rate
limiting (unit + 429 via API), provider error classification, retry/backoff,
no-retry on 4xx, fallback, total timeout budget, concurrency shedding,
HTTP error mapping (502/503/504), `/health` states and metrics recording.

---

## Configuration

Everything is environment-driven (`app/config.py`, `.env.example`). Secrets
have **no defaults in code**: the app refuses to start without `JWT_SECRET`,
`DATABASE_URL` and `LLM_API_KEY`, and bootstrap users are only created from
env-provided passwords.

| Variable | Default | Notes |
|----------|---------|-------|
| `JWT_SECRET` | – (required) | `openssl rand -hex 32` |
| `DATABASE_URL` | – (required) | SQLAlchemy URL |
| `LLM_API_KEY` | – (required) | Provider key |
| `LLM_BASE_URL` | OpenAI | Any OpenAI-compatible URL |
| `LLM_MODEL` / `LLM_FALLBACK_MODEL` | `gpt-4o-mini` / empty | Fallback used after primary retries are exhausted |
| `LLM_TIMEOUT_SECONDS` | 20 | Per upstream call |
| `LLM_TOTAL_TIMEOUT_SECONDS` | 45 | Whole request incl. retries + fallback (below the 60 s proxy timeout) |
| `LLM_MAX_RETRIES` | 3 | Attempts per model (timeouts, 429, 5xx only) |
| `LLM_MAX_CONCURRENCY` / `LLM_QUEUE_TIMEOUT_SECONDS` | 50 / 5 | Per-pod cap on in-flight LLM calls; 503 when no slot frees up |
| `CACHE_TTL_SECONDS` | 300 | Answer cache TTL |
| `RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_GLOBAL_PER_MINUTE` | 60 / 3000 | Per user / cluster-wide |
| `METRICS_TOKEN` | empty | Bearer token for Prometheus; empty = admin JWT only |
| `ADMIN_PASSWORD`, `DEMO_USER_PASSWORD`, `DEMO_READONLY_PASSWORD` | empty | Seed users only when set |
| `LOG_FORMAT` / `LOG_LEVEL` | `json` / `INFO` | Structured logs to stdout |

`.env` is git-ignored; Kubernetes uses a Secret (`k8s/base/secret.example.yaml`
documents the shape only).

---

## Kubernetes

```bash
kubectl kustomize k8s/overlays/prod          # render
kubectl apply -f k8s/base/namespace.yaml
kubectl -n ai-qa-api create secret generic ai-qa-api-secrets \
  --from-literal=JWT_SECRET="$(openssl rand -hex 32)" \
  --from-literal=LLM_API_KEY="sk-..." \
  --from-literal=DATABASE_URL="postgresql+psycopg2://appuser:...@postgres:5432/appdb" \
  --from-literal=ADMIN_PASSWORD="..." \
  --from-literal=METRICS_TOKEN="$(openssl rand -hex 24)"
kubectl apply -k k8s/overlays/prod
```

See [`k8s/README.md`](k8s/README.md).

---

## Repository layout

```
app/
  llm/             provider abstraction (OpenAI-compatible) + gateway
  routers/         auth, chat, admin, reports, metrics
  cache.py         Redis answer cache
  rate_limit.py    sliding-window limiter (Lua, per user + global)
  security.py      JWT (PyJWT) + bcrypt
  deps.py          auth / RBAC / rate-limit dependencies
  middleware.py    request id, latency, HTTP metrics
  logging_config.py JSON logs
  models.py        SQLAlchemy models (users, chat_logs)
tests/             pytest suite
docker/            Dockerfile, compose (single + scaled), nginx.conf
k8s/               Kustomize base + dev/prod overlays (Deployment, HPA, PDB, Ingress…)
terraform/         AWS foundation (VPC, EKS, RDS, ElastiCache, ECR, Secrets Manager)
docs/              architecture diagram, Loom script
frontend/          static web console
```

## License

MIT

# Architecture

This document explains the system's design, reasoning behind each
component, and trade-offs. It complements `README.md` (how to run) and
`SCALING.md` (capacity + migration).

---

## 1. System overview

```
Users -> Load Balancer -> FastAPI Instances -> Redis / Queue -> LLM Gateway -> LLM APIs
                              |
                              +-> PostgreSQL
```

![Architecture diagram](docs/architecture.svg)

See `SCALING.md` for the full under-load diagram.

---

## 2. Components

### 2.1 FastAPI application
- **Role:** stateless HTTP API - authentication, request validation,
  orchestration.
- **Why FastAPI:** async-native, typed request/response via Pydantic,
  automatic OpenAPI, first-class dependency injection for cross-cutting
  concerns (auth, rate limit, cache).
- **Statelessness:** every piece of session state lives in a JWT; every
  shared resource (cache, rate limit, logs) lives in Redis or Postgres.
  Any pod can serve any request - that's what makes horizontal scaling
  trivial.

### 2.2 PostgreSQL
- **Role:** durable store for users and chat logs.
- **Why Postgres:** ACID, mature tooling (Alembic), JSON support if we
  later want to store structured metadata alongside chat logs, and it's
  the default choice for AWS RDS.
- **Schema:** `users` (id, username, email, hashed_password, role,
  is_active, timestamps), `chat_logs` (id, user_id FK, question, answer,
  model, tokens, latency_ms, status, created_at).
- **Indexes:** `users.username` (login lookup), `chat_logs.user_id`
  (history), `chat_logs.created_at` (recent-first ordering).

### 2.3 Redis
Three distinct responsibilities:

1. **Cache** - `chat:answer:<sha256(normalized_question)>` maps to
   serialized `ChatResponse`. TTL default 300s. Cuts LLM cost 10-40x on
   real workloads with repeated questions.
2. **Distributed rate limiting** - sliding-window counter per user and
   global, implemented as an atomic Lua script so it works correctly
   across any number of API pods.
3. **Queue broker (designed, not deployed)** - Celery/RQ/arq for long-running
   or batch LLM jobs; see `SCALING.md` §5.6.

Redis is treated as an *accelerator*, not a hard dependency: short socket
timeouts (0.5 s), cache errors become misses, and the rate limiter fails
open. `/health` reports `degraded` and readiness stays green, so a Redis
outage slows things down instead of taking every pod out of rotation.

**Why Redis for all three:** it's already in the stack, it's fast
(<1 ms for GET/SET), and separating would add operational cost without
benefit at this scale.

### 2.4 LLM Gateway
Abstraction between the API and any LLM provider. Owns:

- **Provider selection** (any OpenAI-compatible API today; `anthropic`,
  `gemini` are one new provider class each).
- **Timeouts** - per upstream call (`LLM_TIMEOUT_SECONDS`) *and* a total
  budget for the whole request including retries + fallback
  (`LLM_TOTAL_TIMEOUT_SECONDS`, 45 s, below the 60 s proxy timeout) → `504`.
- **Retry with exponential backoff + jitter** only on transient errors
  (timeout, 429, 5xx, transport). 4xx client errors (bad key/model/request)
  are *not* retried. A provider `Retry-After` is honoured (capped).
- **Fallback model** - after the primary is exhausted, one pass on the
  fallback model; `fallback_used` is returned explicitly (not inferred from
  the model name, which providers version, e.g. `gpt-4o-mini-2024-07-18`).
- **Concurrency cap** - a per-process semaphore (`LLM_MAX_CONCURRENCY`);
  if no slot frees within `LLM_QUEUE_TIMEOUT_SECONDS` the request is shed
  with `503 + Retry-After` instead of piling up behind a slow provider.
- **Pooled HTTP client** - one keep-alive `httpx.AsyncClient` per process.
- **Token accounting** - prompt/completion tokens per call → metrics + DB.
- **Latency measurement** - histogram per (provider, model).

The router never sees provider-specific details. Adding a provider is one
file.

### 2.5 Observability
- **Prometheus metrics** at `/metrics` (admin JWT, or `METRICS_TOKEN`
  bearer for Prometheus): `api_requests_total`, `api_request_latency_seconds`
  and `api_in_flight_requests` (labelled by route template, bounded
  cardinality), `llm_calls_total{outcome}`, `llm_latency_seconds`,
  `llm_tokens_total{model,type}`, `llm_retries_total`, `llm_fallback_total`,
  `llm_in_flight_requests`, `chat_requests_total{outcome}`, cache
  hit/miss, `rate_limited_total{scope}`.
- **Structured JSON logs** (`LOG_FORMAT=json`) with `request_id`, latency,
  model, tokens.
- **X-Request-ID** and **X-Response-Time-ms** response headers.
- **Usage reports** from PostgreSQL at `/reports/usage` (per-model
  requests, tokens, latency) for admins and read-only analysts.

---

## 3. Authentication and authorization

### 3.1 Current implementation
JWT-based, issued by `POST /auth/login`:

- Password hashing: bcrypt (direct `bcrypt` library; passlib and python-jose were dropped - unmaintained, CVEs).
- JWT library: PyJWT; `exp`, `iat`, `sub`, `iss`, `aud` are required and validated.
- Token payload: `sub` (username), `role`, `iat`, `exp`, `iss`, `aud`.
- Signed with HS256 using `JWT_SECRET`.
- `iss` and `aud` are baked in from day 1 so that swapping to an external
  IdP later doesn't invalidate existing clients.

### 3.2 RBAC (implemented)

| Capability | admin | user | read_only |
|------------|:-----:|:----:|:---------:|
| `POST /chat` | ✅ | ✅ | ❌ 403 |
| `GET /chat/history` (own rows only) | ✅ | ✅ | ✅ |
| `GET /reports/usage` (aggregates, no Q&A text) | ✅ | ❌ | ✅ |
| `GET/POST/PATCH /admin/users` (manage users & roles) | ✅ | ❌ | ❌ |
| `GET /metrics` | ✅ (or scrape token) | ❌ | ❌ |

- Role groups live in one place (`app/deps.py`: `CHAT_ROLES`,
  `REPORT_ROLES`, `ADMIN_ROLES`) and are enforced with
  `Depends(require_role(...))`.
- The role is read from the **database** on every request (not trusted from
  the token), so demoting or disabling a user takes effect immediately
  (disabled users get `403` even with a valid token).
- Admins cannot demote or disable themselves (prevents lock-out).
- Natural extensions: permission strings (`chat:write`, `reports:read`,
  `users:admin`) mapped from roles, so new roles are config not code;
  per-role rate limits and model access (e.g. only admins may use the
  expensive model); tenant/team scoping of reports.

### 3.3 Path to production SSO / OAuth2 / OIDC

```
Application (web/SPA/CLI)
   │ 1. redirect: Authorization Code + PKCE
   ▼
SSO / OAuth2 / OIDC  ──►  Identity Provider (Okta / Entra ID / Auth0 / Keycloak)
   │ 2. user authenticates (MFA, corporate policies)
   │ 3. IdP issues ID token + short-lived access token (JWT, RS256)
   ▼
JWT (claims: sub, iss, aud, exp, groups/roles, scope)
   │ 4. Authorization: Bearer <access token>
   ▼
API Gateway (AWS API GW / Kong / Envoy / NGINX)
   │ 5. validates signature against IdP JWKS, iss, aud, exp
   │ 6. coarse authz + rate limiting per client/tenant
   ▼
AI Service (this FastAPI app)
     7. re-validates the JWT (defence in depth), maps IdP groups → roles,
        applies fine-grained RBAC (require_role) and per-user limits
```

What changes in this codebase:

1. `decode_access_token` switches from HS256 + shared secret to RS256/ES256
   with keys fetched (and cached) from the IdP's `jwks_uri`; `iss`/`aud`
   validation is already in place, so clients are unaffected by the switch.
2. `/auth/login` is removed (or kept only for service accounts); user
   records are provisioned just-in-time from `sub`/`email` on first request
   or via SCIM.
3. A config mapping turns IdP groups/app roles into `admin` / `user` /
   `read_only`; route handlers and `require_role` do not change.
4. Machine-to-machine callers use the OAuth2 client-credentials flow with
   scopes (e.g. `chat:write`).
5. Token revocation: short access-token lifetimes (5-15 min) + refresh
   tokens at the client; the DB `is_active` flag remains an instant kill
   switch.

---

## 4. Scaling design

See `SCALING.md` for the full capacity model, load test plan, and failure
scenarios.

---

## 5. Migration

See `SCALING.md` section 8 for the phased single-EC2 to EKS migration.

---

## 6. Trade-offs

| Decision | Chosen for | Trade-off |
|----------|-----------|-----------|
| Cache in Redis vs in-process | Correctness across pods | Extra network hop (~1 ms) |
| Sliding window vs token bucket | Precision | More memory per user |
| Fail-open on Redis errors | Availability > strict RL | A Redis outage can allow a burst |
| Single Redis for cache + RL + queue | Ops simplicity | Vertical scaling limit |
| HS256 JWT now | Simple local auth | Migration to RS256 when IdP added |
| `/metrics` behind auth | Prevents info leak | Prometheus needs `METRICS_TOKEN` (ServiceMonitor) |
| Redis fail-open + non-fatal readiness | Availability | Brief window without rate limiting |
| One uvicorn worker per pod | Coherent metrics + concurrency cap | More pods for the same CPU |
| Retries in-process vs queue | Lowest latency | Pod restart loses in-flight retries |

---

## 7. What's not built (and why)

- **Vector DB for RAG** - optional in the brief. Adding pgvector is ~1
  day; the abstraction point is `LLMGateway.answer` (add a `retrieve()`
  step before `complete()`).
- **Queue workers** - designed (`SCALING.md` §5.6) but not deployed,
  because `/chat` is synchronous and bounded by the 45 s budget.
- **Circuit breaker** - retries + fallback + total budget + concurrency cap
  cover the assessment. A Redis-backed breaker (skip the primary for N
  seconds after M consecutive failures) is a small addition to
  `LLMGateway._answer`.
- **Distributed token-bucket for provider TPM/RPM** - per-pod concurrency is
  capped today; a cluster-wide Redis token bucket per API key is described in
  `SCALING.md` §5.7.
- **Alembic migrations** - tables are created idempotently at startup
  (`DB_AUTO_CREATE`); a real pipeline would run `alembic upgrade head` as a
  pre-deploy Job.
- **mTLS between services** - overkill for a single-cluster demo.

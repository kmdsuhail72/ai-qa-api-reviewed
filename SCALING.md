# Scaling & Migration Design

**Scope:** Assessment section 4 (scaling to 100 to 500 RPS) and section 5
(migrating from a single EC2 to production).

Every number has a stated assumption. Capacity is controlled by
configuration (replicas, HPA targets, rate limits, cache TTLs) - not code.

---

## 1. Executive summary

At 100 RPS sustained, 3 replicas with a 40% cache hit rate keep p95 well
below the LLM's own latency. At 500 RPS bursts, the HPA scales to about
6-10 pods in ~90 seconds, cache absorbs the repeated-question tail, the
per-user rate limiter caps abuse, a cluster-wide Redis limiter
(`RATE_LIMIT_GLOBAL_PER_MINUTE`) caps total admitted chat traffic regardless
of pod count, and a per-pod concurrency cap on LLM calls sheds excess load
with `503 + Retry-After` instead of queueing behind a slow provider.

The single-EC2 to production migration is phased: containerize, provision
managed data services, dark-launch, shift traffic by weighted ALB rules,
decommission. Zero downtime. Every step reversible.

---

## 2. Traffic model

| Assumption | Value |
|------------|-------|
| Steady-state RPS | 100 |
| Peak RPS | 500 |
| Peak duration | 2-5 minutes |
| Avg question length | 40 tokens |
| Avg answer length | 120 tokens |
| Cache hit rate | 40% |
| LLM p50 | 800 ms |
| LLM p95 | 2.5 s |

**Derived:**
- Peak LLM QPS = 500 x (1 - 0.40) = **300 calls/s**
- Peak token rate = 300 x 160 = **48,000 tokens/s**
- Steady-state token rate = 100 x 0.6 x 160 = **9,600 tokens/s**

---

## 3. Capacity math

### Provider tier mapping

| Tier | RPM | TPM | Concurrent |
|------|-----|-----|------------|
| 1 | 500 | 30 K | 20 |
| 2 | 5,000 | 450 K | 100 |
| 3 | 10,000 | 2 M | 500 |
| 4 | 30,000 | 10 M | 2,000 |

**At 500 RPS peak**, we need 18,000 RPM and ~2.9 M TPM. That requires
**Tier 3 with 2 API keys** or **Tier 4**.

**Implication:** without caching and rate limiting, the app is bottlenecked
by the provider, not the pods. Cache + per-user limit are what make the
target RPS achievable on a reasonable provider tier.

### Pod capacity

| Metric | Per pod |
|--------|---------|
| CPU requests | 250m |
| CPU limits | 1000m |
| Sustained RPS (cached) | ~400 |
| Sustained RPS (LLM) | ~80 |

**At 500 RPS with 40% cache hit:**
- LLM QPS = 300 implies ~4 pods for LLM path
- With 65% CPU headroom, HPA settles at **6-10 pods**

---

## 4. Architecture under load

```
Users -> ALB -> NGINX Ingress -> FastAPI Pods (HPA 3 to 30)
                                      |
                    +-----------------+-----------------+
                    v                 v                 v
                Redis (cache+RL)  Postgres        LLM Gateway
                                                       |
                                        +--------------+--------------+
                                        v              v              v
                                    Key A          Key B       Fallback model
                                        |              |              |
                                        +--------------+--------------+
                                                       v
                                                   LLM APIs
```

**Request lifecycle:**
1. ALB terminates TLS, WAF rules, forwards to ingress.
2. NGINX enforces coarse per-IP limit-rps and routes to a pod.
3. FastAPI checks the Redis sliding-window limiter (user + global).
   Over limit returns 429 with Retry-After.
4. Cache lookup by sha256(normalized question). Hit returns in ~10 ms.
5. Miss goes to LLMGateway.answer(): concurrency slot, 15 s per-call
   timeout, up to 3 attempts on transient errors, fallback model, 45 s total
   budget.
6. Persist ChatLog, write cache entry (TTL 300s), return.

Under peak, steps 3-4 protect the provider. ~40% of requests never reach
step 5, and every request that does has already passed two rate-limit
checks.

---

## 5. Scaling mechanisms

### 5.1 Horizontal scaling
FastAPI is stateless. Every session token is JWT; every shared resource
is Redis or Postgres. replicas: N is a safe operation.

**Bound by:** Redis connection count (50/pod x 30 pods = 1,500; Redis
maxclients default 10,000 OK). Postgres pool (5 + 10 overflow x 30 pods =
450; RDS db.t4g.medium has max_connections ~340 - add PgBouncer at >20 pods).

### 5.2 Load balancing
ALB (L7) at the edge, NGINX ingress inside. Both use /health/ready.
- ALB: 10s interval, 3 failures marks unhealthy.
- Readiness probe: 10s interval, 3 failures.
- preStop: sleep 10 ensures in-flight requests complete.

### 5.3 Kubernetes HPA
k8s/base/hpa.yaml: CPU 65%, memory 75%, 3-30 replicas, 30s up / 300s down.

**Why 65% CPU:** LLM response latency is sensitive to CPU contention. At
80% CPU, p99 roughly doubles under the same RPS.

**When CPU isn't enough:** if the app is I/O-bound (waiting on LLM), CPU
stays low. Add a custom metric via Prometheus Adapter:

```yaml
metrics:
  - type: Pods
    pods:
      metric: { name: api_in_flight_requests }
      target: { type: AverageValue, averageValue: "20" }
```

### 5.4 Redis cache
chat:answer:<sha256(normalized question)> maps to serialized ChatResponse.
TTL 300s.

**Hit-rate estimate:** 40% on consumer Q&A, 10-20% on code assistants.

**Impact at 500 RPS:**
- 200 RPS served in ~10 ms (Redis GET + deserialize).
- 300 RPS hit the LLM.
- Provider load reduced from 80 K to 48 K tokens/s (≈4.8 M → 2.9 M TPM) -
  the difference between needing Tier 4 and Tier 3 with two keys.

**Memory:** 300s x 300 RPS = 90,000 entries x ~1 KB = ~90 MB at steady
state. maxmemory 256mb in compose.

### 5.5 Redis - rate limiting
Sliding-window counter, atomic Lua script. One key per user plus one global.
The global limit must be sized to the provider budget, not left at the
local default: `RATE_LIMIT_GLOBAL_PER_MINUTE=3000` (50 RPS) is the dev value;
the prod ConfigMap sets **36000/min (600 RPS)** so a 500 RPS peak is admitted
while a runaway client/bug still hits a ceiling. (The global check runs
before the per-user check so a globally rejected request doesn't consume
the user's quota.)

**Why sliding window, not fixed:**
- Fixed window allows 2x burst at the boundary (60 at :59, 60 at :00).
- Sliding window smooths it: at most 60 in any 60-second span.

**Fail-open on Redis outage:** availability beats strict enforcement. NGINX
limit-rps: 200 still provides a coarse cap.

### 5.6 Background queues
Not needed for synchronous /chat. Add when:
- LLM p95 exceeds client patience (~5s).
- Batch operations appear.
- Retries need to survive pod restarts.

**Design:** POST /chat/async enqueues job, returns 202 + job_id. Celery
workers process. GET /chat/jobs/{id} polls result.

**Why Celery + Redis, not Kafka:** Redis is already a broker; Kafka adds
Zookeeper/KRaft weight for a feature that needs at-least-once with retries,
not streaming.

### 5.7 LLM provider limits (RPM, TPM, concurrency)
Three separate caps, all provider-enforced, all returning 429.

**Implemented:**
- Per-pod concurrency semaphore (`LLM_MAX_CONCURRENCY`, default 50) around
  every upstream call. Cluster concurrency = pods × cap, so the cap is set
  from the provider's concurrency limit ÷ max replicas. Waiting longer than
  `LLM_QUEUE_TIMEOUT_SECONDS` → `503 + Retry-After` (load shedding).
- 429 from the provider is retried with backoff that honours `Retry-After`,
  then falls back to the secondary model (often a different quota bucket),
  then returns `503 + Retry-After` to the client.
- Cluster-wide admission cap via the global Redis limiter (§5.5).

**Next step (designed):** a Redis token bucket per provider key that
debits *estimated tokens* (prompt length + `max_tokens`) before the call and
refunds the difference after, so TPM is enforced cluster-wide, not just RPM.
Multiple keys: if one key's bucket is empty, try the next (round-robin with
failover); keys stored in the Secret indexed by number.

### 5.8 Concurrent requests
One shared, keep-alive `httpx.AsyncClient` per process
(`LLM_HTTP_MAX_CONNECTIONS=100`), created once and closed on shutdown.
- 1 uvicorn worker per pod; scale with pods. (Multiple workers would give
  each process its own Prometheus registry and its own concurrency cap.)
- The LLM call is fully async; blocking work (SQLAlchemy writes, sync Redis
  cache calls) runs in the threadpool so it never stalls the event loop.
- Each pod holds hundreds of concurrent requests waiting on the LLM; the
  semaphore bounds how many are actually in flight upstream.

A single pod with 250m CPU serves ~80 LLM RPS before latency degrades.

### 5.9 Failure recovery

| Layer | Mechanism | Bounds |
|-------|-----------|--------|
| Per-call timeout | httpx timeout | `LLM_TIMEOUT_SECONDS` (15 s in prod ConfigMap) |
| Retry | Exponential backoff + jitter, honours `Retry-After` | 3 attempts/model; only timeout, 429, 5xx, transport errors |
| No retry | 4xx client errors (bad key / model / payload) | fail fast, go to fallback |
| Fallback | Secondary model (cheaper / separate quota) | one pass after primary exhausted |
| Total budget | `asyncio.wait_for` around the whole flow | `LLM_TOTAL_TIMEOUT_SECONDS` = 45 s (< 60 s ingress timeout) |
| Load shedding | Concurrency semaphore | 503 + Retry-After after 5 s wait |
| Terminal | 502 (provider error), 503 (provider 429 / shed), 504 (timeout), all with `Retry-After` | client retries with backoff |

**Retry budget:** without the total budget, 3 × (20 s + 4 s) on the primary
plus the same on the fallback could reach ~144 s - far past the 60 s proxy
timeout, so clients would see a cut connection while the pod kept working.
The 45 s total budget guarantees a clean `504` first.

### 5.10 Graceful degradation ladder

| Condition | Behavior |
|-----------|----------|
| LLM 5xx | Retry, fallback model, then 502 + Retry-After |
| LLM timeout | Retry, fallback, then 504 (bounded by 45 s budget) |
| Provider 429 | Backoff honouring Retry-After, fallback, then 503 + Retry-After |
| Pod saturated (LLM slots full) | 503 + Retry-After after 5 s; HPA adds pods |
| Redis down | Cache misses; RL fails open; `/health` = `degraded` (200); pods stay ready |
| Postgres down | Login/auth impossible → `/health` 503, readiness fails; if the DB drops mid-request the answer is still returned and the audit write is skipped (logged + `chat_logs_written_total{status="db_error"}`) |
| All providers down | Cached answers are still served (cache is checked before the LLM); misses get 502/504 |

---

## 6. Load test plan (k6)

```javascript
import http from "k6/http";
import { check } from "k6";

export const options = {
  stages: [
    { duration: "1m", target: 100 },
    { duration: "5m", target: 100 },
    { duration: "1m", target: 500 },
    { duration: "3m", target: 500 },
    { duration: "1m", target: 0 },
  ],
  thresholds: {
    http_req_duration: ["p(95)<3000"],
    http_req_failed: ["rate<0.01"],
  },
};

const TOKEN = __ENV.TOKEN;

export default function () {
  const payload = JSON.stringify({ question: "What is 2+2?" });
  const headers = {
    "Content-Type": "application/json",
    Authorization: "Bearer " + TOKEN,
  };
  const res = http.post(__ENV.BASE_URL + "/chat", payload, { headers });
  check(res, { "status is 200": (r) => r.status === 200 });
}
```

**Success criteria:**
- p95 < 3s at 100 RPS.
- p95 < 5s at 500 RPS.
- Error rate < 1%.
- HPA settles at 15 pods or fewer at 500 RPS.

---

## 7. Failure scenarios

| Scenario | Blast radius | Detection | Response |
|----------|-------------|-----------|----------|
| 1 pod crashes | None (2 remain) | Liveness, restart metric | Pod restarts, preStop drains |
| Node drain | ~25% capacity | Cluster events | HPA adds replicas |
| Redis outage | Cache misses, RL fail-open | `/health` degraded, `rate_limit_redis_error` logs | Requests slower; alerts fire; pods stay in rotation |
| Postgres outage | Auth fails (all authenticated endpoints) | `/health` 503, readiness 503 | Page on-call; RDS Multi-AZ failover (~60-120 s) |
| LLM 5xx | Fallback model | `llm_calls_total{outcome="error"}`, `llm_fallback_total` | Auto-recover or 502 |
| LLM rate limit | Requests wait / 503 | Retry + provider 429s | Token bucket refills |
| Zone failure | 33% capacity | CloudWatch, cluster events | Multi-AZ ALB routes healthy |
| Traffic spike 10x | Provider cap | HPA + provider 429s | RL limits damage; cache serves rest |
| DDoS | Edge saturation | WAF, ALB metrics | WAF blocks; RL returns 429 |

---

## 8. Migration: single EC2 to ECS/EKS

### Current state
- 1 EC2 running uvicorn.
- SQLite or self-managed Postgres on same instance.
- No cache, no queue, no metrics.
- ~10 users.

### Pain points at 10,000 users
1. Single point of failure.
2. Vertical ceiling.
3. State coupling (DB loses data on instance failure).
4. No isolation (slow LLM blocks web workers).
5. No observability.

### Target architecture

```
Users -> Route 53 -> CloudFront + WAF -> ALB -> EKS/ECS -> ElastiCache
                                              |
                                              v
                                          RDS Postgres
                                              |
                                              v
                                          Backups
                                              |
                                              v
                                          LLM Gateway -> LLM APIs
```

| Component | Choice | Why |
|-----------|--------|-----|
| Compute | EKS (or ECS Fargate) | EKS for portability; ECS for less ops |
| DB | RDS Postgres Multi-AZ | Managed backups, failover, patching |
| Cache | ElastiCache Redis | Managed, cluster mode available |
| CDN/WAF | CloudFront + AWS WAF | DDoS protection |
| Secrets | Secrets Manager + IRSA | No long-lived keys |
| Observability | Prometheus + Grafana Cloud | Managed |
| Queue | ElastiCache Redis + Celery | Dev/prod parity |

### Phased migration with zero downtime

| Phase | Duration | Action | Downtime |
|-------|----------|--------|----------|
| 0 | 1 day | Containerize existing app; verify locally | 0 |
| 1 | 1 day | Provision RDS, ElastiCache, ECR | 0 |
| 2 | 1 day | Migrate schema; dual-write from EC2 | 0 |
| 3 | 2 days | Run both stacks; mirror 1% traffic; compare | 0 |
| 4 | 1 day | Shift ALB weights: 1% to 10% to 50% to 100% | 0 |
| 5 | 1 week | Old EC2 idle in standby | 0 |
| 6 | 1 day | Decommission EC2 | 0 |

**Rollback at any step:** flip ALB weights back. Old stack unchanged until
Phase 6.

**Data gotchas:**
- Dual-write window: verify with periodic count parity.
- ID sequences: use UUIDs, or set nextval in new DB beyond old DB max.
- Auth tokens: keep JWT_SECRET stable, or add token_version + grace window.

### Secrets and config
- Anti-pattern: JWT_SECRET in a .env on EC2, SSH'd to.
- Target: Secrets Manager + IRSA + External Secrets Operator. Pod mounts
  secret as env vars, never on disk. Rotate via Lambda; rolling restart
  picks up new value.

---

## 9. Monitoring, alerting, SLOs

| SLI | SLO | Alert if |
|-----|-----|----------|
| Availability | 99.9% | 5xx rate > 1% for 5 min |
| Latency (p95) | < 3s at 100 RPS | p95 > 3s for 5 min |
| Latency (p99) | < 8s | p99 > 8s for 5 min |
| LLM success | > 98% | failure rate > 2% for 5 min |
| Cache hit rate | > 30% | < 20% for 30 min |
| Rate-limit rejection | < 1% of traffic | > 5% for 5 min |

Prometheus alert rules (excerpt):

```yaml
- alert: HighErrorRate
  expr: sum(rate(api_requests_total{status=~"5.."}[5m])) / sum(rate(api_requests_total[5m])) > 0.01
  for: 5m

- alert: HighLatency
  expr: histogram_quantile(0.95, sum(rate(api_request_latency_seconds_bucket[5m])) by (le)) > 3
  for: 5m

- alert: LLMFailureRate
  expr: sum(rate(llm_calls_total{outcome="error"}[5m])) / sum(rate(llm_calls_total[5m])) > 0.02
  for: 5m

- alert: LLMFallbackSpike
  expr: sum(rate(llm_fallback_total[10m])) / sum(rate(chat_requests_total{outcome=~"success|llm_.*"}[10m])) > 0.10
  for: 10m

- alert: LoadShedding
  expr: sum(rate(chat_requests_total{outcome="llm_overloaded"}[5m])) > 0
  for: 5m
```

Dashboards:
- Overview - traffic, latency, errors, cache hit ratio, tokens.
- LLM health - per-model call rate, retry, fallback, tokens.
- Capacity - HPA replicas, CPU/memory, node count.
- Cost - tokens/day x rate to estimated daily spend.

---

## 10. Trade-off matrix

| Decision | Chosen for | Trade-off | Reconsider when |
|----------|-----------|-----------|-----------------|
| In-process retries | Latency | Pod restart loses retries | Retry > 5% |
| Redis for cache + RL + queue | Simplicity | Vertical ceiling | Redis > 60% memory |
| Sliding-window RL | Precision | More memory | Users > 100k |
| 65% CPU HPA | Latency headroom | Higher cost | SLOs relaxed |
| Fallback to cheaper model | Availability | Quality drop | Fallback > 10% |
| HS256 JWT | Simplicity | Shared secret = SPOF | Any external IdP |
| No service mesh | Lower ops | No mTLS | > 5 services |
| Single RDS | Cost | Vertical ceiling | DB CPU > 70% |
| Cache TTL 300s | Bounded staleness | Stale answers | Compliance |
| Queue deferred | Not needed for sync | No async support | Any endpoint > 5s p95 |

---

## 11. What I'd do differently at 10x scale

At 5,000 RPS sustained (10x peak):

1. Two Redis clusters - cache (allkeys-lru) vs RL+queue (AOF).
2. PgBouncer between app and Postgres.
3. Queue-first /chat - enqueue + poll.
4. Multi-region active-active with global rate limit in DynamoDB
   Global Tables.
5. Custom LLM router - route by cost/quality based on prompt length
   and past evaluation.
6. Semantic cache - embeddings + vector DB. Raises hit rate from 40%
   to 60%+.
7. Reserved capacity with provider, or self-hosted vLLM for cheap
   high-volume traffic.

Each is a project of its own. The current design doesn't preclude any of
them - every abstraction (gateway, cache, limiter, queue) is at the right
seam.

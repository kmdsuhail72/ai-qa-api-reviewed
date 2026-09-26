# Loom Video Script — AI/LLM Platform & DevOps Engineer Assessment

**Target length:** 4:45
**Recording tool:** Loom (screen + optional webcam)

---

## [0:00 - 0:20] Intro

> Hi, I'm <name>. This is my submission for the AI/LLM Platform & DevOps
> Engineer assessment.
>
> In the next five minutes I'll walk through: the repository, a live demo
> of the running stack, the architecture and resilience choices, and how
> the system scales from 100 to 500 requests per second.

*[On screen: GitHub repo landing page, README visible.]*

---

## [0:20 - 0:50] Repo tour

> This is a FastAPI service that authenticates users, calls an LLM, caches
> answers in Redis, persists chat logs to Postgres, and exposes Prometheus
> metrics behind role-based access control.
>
> The repo has app/ for the FastAPI code, tests/ for the pytest suite,
> docker/ for the container and Compose stack, k8s/ for Kubernetes
> manifests, and docs/ for this script.
>
> README.md is the entry point; ARCHITECTURE.md explains the design;
> SCALING.md covers capacity and migration.

*[Scroll through the tree, open README, then ARCHITECTURE briefly.]*

---

## [0:50 - 2:20] Live demo

> Let me show it running. The stack is already up via docker compose.

*[Terminal: `docker compose --env-file .env -f docker/docker-compose.yml ps` - all healthy.]*

> That's the API, Postgres, and Redis - all containers healthy.
>
> First, the login endpoint issues a JWT.

*[Run login curl, show token.]*

> Next, the chat endpoint. It authenticates the token, checks the per-user
> rate limit, checks the Redis cache, and on a miss calls the LLM with
> retries and fallback.

*[Run first chat curl, show answer.]*

> Note the response includes the model, token counts, and latency.
>
> Now the same question again.

*[Run same curl again.]*

> Same answer, but cached: true this time, and the LLM was never called.
> That's the Redis cache doing its job. The second request returned in
> around 50 milliseconds instead of over a second.
>
> Let me trigger the rate limiter by hitting the endpoint past the
> per-minute limit.

*[Run a short loop, or mention that requests past 60/min return 429 with Retry-After.]*

> The response includes Retry-After and X-RateLimit-* headers.
>
> Finally, the metrics endpoint. This is admin-only.

*[Run `curl localhost:8000/metrics -H "Authorization: Bearer $TOKEN" | head -30`]*

> All the counters we care about: requests, latency histograms, LLM calls,
> tokens, cache hits and misses, rate-limited requests.

---

## [2:20 - 3:20] Architecture and resilience

*[Open ARCHITECTURE.md, scrolled to section 2.]*

> Requests come in through a load balancer and NGINX ingress, hit one of
> N stateless FastAPI pods, and each pod talks to Redis for cache and
> rate limiting, Postgres for persistence, and an LLM gateway for the
> model call.
>
> Three things make this resilient. First, the LLM gateway: retries with
> exponential backoff on transient errors, and a fallback model if the
> primary keeps failing. Second, the Redis sliding-window rate limiter,
> implemented as an atomic Lua script so it's correct across all pods.
> Third, the cache - an exact-hash cache with a five-minute TTL.
>
> Every request is logged with a request_id, and every metric I showed is
> visible in Prometheus format.

*[Switch to app/llm/gateway.py - show the retry + fallback block.]*

> This is the retry and fallback logic. Notice the jitter on the backoff -
> that prevents a thundering herd if the provider has a bad minute.

---

## [3:20 - 4:20] Scaling and migration

*[Open SCALING.md.]*

> Now scaling.
>
> At 100 requests per second sustained, three pods are enough. At 500 RPS
> peak, the HPA scales to roughly six to ten pods based on CPU and memory.
> The cache absorbs about forty percent of the traffic, so the LLM provider
> sees about three hundred calls per second - not five hundred.
>
> The pods are stateless, so horizontal scaling is just a replica count.
> The per-user rate limiter caps abuse; a global limiter caps provider
> load. A token bucket keyed on the provider's RPM, TPM, and concurrency
> limits lives in Redis so all pods share it.
>
> For failures: timeouts at twenty seconds, three retries with backoff,
> a fallback model, and a terminal 502 with Retry-After if everything
> fails.
>
> The migration from a single EC2 is phased: containerize, provision
> managed RDS and ElastiCache, dual-write, dark-launch one percent of
> traffic, then shift ALB weights gradually. The old EC2 stays warm for a
> week as a rollback option. No step requires downtime.

---

## [4:20 - 4:45] Wrap-up

> That's the project. Everything is in the repo: the code, the 67-test
> pytest suite, the Compose stack, the Kubernetes manifests, and the
> design documents.
>
> Thanks for watching.

*[Show README.md top with links.]*

---

## On-camera tips

- Do not read the script verbatim. Rehearse until natural.
- Cut dead air in Loom's editor.
- Every claim should appear on screen within 5 seconds.
- One idea per sentence.
- Hard-stop at 5:00.

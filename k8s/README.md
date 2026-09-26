# Kubernetes deployment

Kustomize layout:

```
k8s/
  base/                      namespace, serviceaccount, configmap, deployment,
                             service, ingress, hpa, pdb
  base/secret.example.yaml   shape of the Secret (NOT applied; create it out of band)
  base/servicemonitor.example.yaml  Prometheus Operator scrape config (needs the CRD)
  overlays/dev               local image, no HPA
  overlays/prod              6 replicas baseline, HPA 3 → 30
```

## What the app expects from the cluster

| Dependency | How it is wired |
|------------|-----------------|
| PostgreSQL | `DATABASE_URL` in the `ai-qa-api-secrets` Secret (RDS in prod, see `terraform/`) |
| Redis | `REDIS_URL` in the ConfigMap (in-cluster Redis, or `rediss://` for ElastiCache with TLS) |
| LLM provider | `LLM_API_KEY` in the Secret, `LLM_BASE_URL` / models in the ConfigMap |
| Ingress | NGINX Ingress Controller (`ingressClassName: nginx`) |
| Metrics | Prometheus Operator (`servicemonitor.example.yaml`) using `METRICS_TOKEN` |
| HPA | metrics-server (CPU/memory); optional Prometheus Adapter for in-flight-request scaling |

Postgres and Redis are intentionally **not** deployed by these manifests -
in production they are managed services (RDS / ElastiCache) provisioned by
`terraform/`.

## Deploy

```bash
kubectl apply -f k8s/base/namespace.yaml
kubectl -n ai-qa-api create secret generic ai-qa-api-secrets \
  --from-literal=JWT_SECRET="$(openssl rand -hex 32)" \
  --from-literal=LLM_API_KEY="sk-..." \
  --from-literal=DATABASE_URL="postgresql+psycopg2://appuser:...@<rds-endpoint>:5432/appdb?sslmode=require" \
  --from-literal=ADMIN_PASSWORD="..." \
  --from-literal=METRICS_TOKEN="$(openssl rand -hex 24)"
kubectl apply -k k8s/overlays/prod
kubectl -n ai-qa-api rollout status deploy/ai-qa-api
```

In a real pipeline the Secret comes from AWS Secrets Manager through External
Secrets Operator + IRSA rather than `kubectl create secret`.

## Probes and rollout safety

- `startupProbe` / `livenessProbe` → `/health/live` (process only; never
  checks shared dependencies, so a Redis blip can't restart every pod).
- `readinessProbe` → `/health/ready` (fails only if the database is
  unreachable; Redis down = `degraded` but still ready, because the app keeps
  serving without cache and with a fail-open rate limiter).
- `RollingUpdate` with `maxUnavailable: 0`, `preStop: sleep 10`, uvicorn
  graceful shutdown 25 s < `terminationGracePeriodSeconds: 30`.
- `PodDisruptionBudget minAvailable: 2` for node drains / upgrades.
- One uvicorn worker per pod (`UVICORN_WORKERS=1`): scale with pods, which
  keeps Prometheus metrics and the LLM concurrency cap per process coherent.

## Validate locally

```bash
kubectl kustomize k8s/overlays/prod | kubeconform -strict -summary
```

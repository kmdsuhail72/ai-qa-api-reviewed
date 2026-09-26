# AWS production infrastructure

This directory provisions the managed AWS foundation described in
[`../SCALING.md`](../SCALING.md):

- Multi-AZ VPC with private application/data subnets and NAT gateways
- EKS with an on-demand managed node group
- Immutable, scan-on-push ECR repository
- Encrypted Multi-AZ RDS PostgreSQL
- Encrypted, Multi-AZ ElastiCache Redis replication group
- Secrets Manager entry for application secrets

Terraform does not deploy the Kubernetes application manifests. That remains
an explicit release step so image promotion and infrastructure changes can be
rolled back independently.

## Usage

```bash
cd terraform
terraform init
cp terraform.tfvars.example terraform.tfvars
# Replace every placeholder in terraform.tfvars. Do not commit that file.
terraform fmt -check
terraform plan
terraform apply

aws eks update-kubeconfig \
  --region "<your-region>" \
  --name "$(terraform output -raw eks_cluster_name)"   # or: terraform output -raw kubeconfig_command
kubectl apply -k ../k8s/overlays/prod
```

The RDS password and application secrets are stored in Terraform state because
the resources are managed by Terraform. Use an encrypted remote backend with
restricted access in CI before applying this configuration in a shared
environment. The generated `terraform.tfvars` is intentionally ignored.

The Kubernetes manifests currently expect a Kubernetes Secret named
`ai-qa-api-secrets`. Wire that Secret from the Secrets Manager value using
External Secrets Operator or your deployment pipeline; do not copy secrets
into Git.

### Wiring the managed data stores into the app

- ElastiCache is created with **in-transit encryption**, so the app must use
  TLS: `REDIS_URL=rediss://<redis_primary_endpoint>:6379/0` (note the double
  `s`). Override `REDIS_URL` in the ConfigMap / an overlay accordingly - the
  base ConfigMap points at an in-cluster Redis service.
- `DATABASE_URL=postgresql+psycopg2://<db_username>:<db_password>@<rds_endpoint>:5432/<db_name>?sslmode=require`
  goes into the `ai-qa-api-secrets` Secret (via External Secrets Operator).

> Note: this Terraform was reviewed but not `terraform validate`d in the
> review environment (HashiCorp registry not reachable). Run
> `terraform init && terraform validate` before use.

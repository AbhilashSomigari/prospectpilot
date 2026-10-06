# Deploying ProspectPilot to AWS

The Terraform in `infra/terraform` provisions:

| Resource | Purpose |
|---|---|
| VPC, 2 public + 2 private subnets | Fargate tasks in public subnets with locked-down security groups (no NAT gateway cost); RDS in private subnets |
| ECR repository | the single `prospectpilot` image (api, worker and improve use the same image) |
| RDS Postgres 16 (encrypted, private) | prospect memory, learnings, prompt registry, changelog, LangGraph checkpoints; pgvector is enabled by the first migration |
| Secrets Manager | `DATABASE_URL`, `ANTHROPIC_API_KEY`, `API_KEY` (values for the last two are set out-of-band) |
| ECS Fargate: `api` service behind an ALB, `worker` service | the API runs migrations on start; the worker runs queued campaign runs, queued improvement rounds and due follow-ups |
| Mailpit sidecar in every task | sending stays sandboxed in the cloud: `SMTP_HOST=localhost`, `ALLOW_REAL_SEND=false` |
| EventBridge Scheduler | runs the `improve` task definition nightly (`cron(0 3 * * ? *)` UTC by default) |
| CloudWatch log groups | api, worker, improve, mailpit (30-day retention) |

It is validated (`make tf-validate`) but has **not** been applied by this project.

## Steps

```bash
cd infra/terraform
terraform init                       # configure an S3 backend first for anything shared
terraform plan -out tf.plan          # review: RDS, ALB and Fargate cost money while running
terraform apply tf.plan

# build and push the image
REPO=$(terraform output -raw ecr_repository_url)
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%/*}"
docker build -f ../../docker/Dockerfile -t "$REPO:latest" ../..
docker push "$REPO:latest"

# set the secrets that Terraform deliberately does not hold
aws secretsmanager put-secret-value --secret-id prospectpilot-dev/ANTHROPIC_API_KEY --secret-string "$ANTHROPIC_API_KEY"
aws secretsmanager put-secret-value --secret-id prospectpilot-dev/API_KEY --secret-string "$(openssl rand -hex 24)"

# roll the services onto the new image
aws ecs update-service --cluster prospectpilot-dev --service api --force-new-deployment
aws ecs update-service --cluster prospectpilot-dev --service worker --force-new-deployment
curl "$(terraform output -raw api_url)/healthz"
```

## Notes and next steps

- Observability: `OTEL_ENABLED=false` in the task definitions. To trace in AWS add an ADOT
  collector sidecar exporting to X-Ray (or point `OTEL_EXPORTER_OTLP_ENDPOINT` at a collector);
  scrape `/metrics` with Amazon Managed Prometheus.
- The ALB listener is HTTP; add an ACM certificate + HTTPS listener and set `allowed_api_cidrs`
  before exposing the API.
- The improve task reads/writes the same database, so promoted prompts take effect for the next
  campaign run without a deploy.
- Tear down with `terraform destroy` (RDS skips the final snapshot outside `prod`).

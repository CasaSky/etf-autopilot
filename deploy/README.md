# AWS paper deployment with Terraform

**Validated locally and smoke-tested in an isolated AWS paper deployment.** Each
installation needs an explicit AWS profile/account, region and monthly budget,
plus an alert route before unattended operation. No broker credentials,
permanent broker budget, model permissions or money are configured by this module.
Use only this repository's `deploy/` root and its `etf-autopilot-paper` resources.
Terraform replaces the earlier SAM template; there is one infrastructure definition.

The deployed smoke check completed one synthetic event, replayed it without changing
the ledger, reconciled all three comparison accounts and verified operational paper
health, encrypted state and log delivery. The health schedule stayed disabled.
This verifies deployment/accounting behavior; investment performance and AI effects
are still untested with real market data.

## Resources and limits

Terraform creates one Python 3.13 Lambda, a private AES256-encrypted,
versioned S3 bucket, an SQS failure queue, seven-day CloudWatch logs, scoped IAM
roles and a **disabled** daily EventBridge Scheduler health check. The worker has
256 MB, a 30-second timeout and reserved concurrency one. Async Lambda and
Scheduler automatic retries are zero. S3 SDK attempts are explicitly one. The
Scheduler role is restricted to this account and its dedicated schedule group.

The worker can read its fixed input/state keys, conditionally write its one state
object, list its own bucket (so missing keys produce a detectable 404), write its
own logs and send failures to its queue. It has no SSM/Secrets Manager, Bedrock,
GitHub or unrelated application permissions. No VPC/NAT, EC2, database instance,
AgentCore Gateway or always-on service is provisioned.

The health schedule runs at 18:00 Europe/Berlin if explicitly enabled. It only
checks persisted paper state. New investment decisions require an explicitly
uploaded synthetic input event and a finite worker invocation. Live market data
and financial production scheduling remain later work. The architecture supports
Scheduler → Lambda with S3 CAS persistence, not a GitHub scheduled trading loop.

The source also provides a finite `cycle` action: it reads that same fixed input,
checks freshness against the runtime clock, chooses the configured rule without a
model, runs deterministic checks/reconciliation and returns a report. It never
rewrites old input dates or automatically clears a halt. It is not connected to
the health schedule. See [bounded paper decisions](../docs/AI_DECISIONS.md) for
local use, duplicate handling and the remaining verified-feed/alert prerequisites.

## Reviewable cost estimate

Planning estimate, 25 October 2026, without free-tier credit. Compute, state-storage
and log rates were checked against AWS's public Frankfurt catalogs. The rounded USD
unit rates below remain conservative **budget assumptions rather than price quotes**;
recheck the selected region and workload before each deployment.

| Item | Assumed monthly quantity | Planning unit rate | Estimate USD/month |
|---|---:|---:|---:|
| Lambda duration | 70 invocations × 30s × 0.25GB = 525 GB-s | 0.000025/GB-s | 0.0132 |
| Lambda requests | 70 | 0.30/million | <0.0001 |
| S3 requests | 110 GET + 40 PUT | 0.0005/1k GET, 0.006/1k PUT | <0.0004 |
| S3 state/input/artifact storage | 0.25GB incl. old versions | 0.03/GB-month | 0.0075 |
| CloudWatch ingestion/storage | 0.01GB + seven-day retention | 0.70/GB ingest; 0.04/GB-month | <0.0075 |
| Scheduler/SQS | 31 health calls; small failure volume | 2/million calls; small queue allowance | <0.001 |
| Bedrock / broker requests | zero | disabled | 0 |

Expected small paper workload: roughly **USD 0.03/month** under these assumptions;
reserve **EUR 1/month** as an initial application planning allowance after confirming
it with the user. Currency conversion, tax, deployment artifacts, other account
resources, extra requests and optional alarms/notifications are not included. This
is not an account-wide or adversarial worst-case billing cap. No broker trading
fees are incurred by paper runs. Future real orders budget EUR 1/order as a simulation
assumption until actual quotes establish costs; native savings-plan promotions
are not assumed for API orders.

Application checks cap optional model calls, estimated spend and new market
events; they cannot prevent infrastructure charges from repeated invocations.
Do not enable broad public invocation. Before unattended use, review an actual
AWS Pricing Calculator estimate and add the chosen budget/error notifications.
No notification destination or paid alarm is silently created here.

Sources: [Lambda pricing](https://aws.amazon.com/lambda/pricing/),
[S3 pricing](https://aws.amazon.com/s3/pricing/),
[EventBridge pricing](https://aws.amazon.com/eventbridge/pricing/),
[Scheduler and Lambda](https://docs.aws.amazon.com/lambda/latest/dg/with-eventbridge-scheduler.html),
[S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html).

Catalog checks: [Frankfurt Lambda](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSLambda/current/eu-central-1/index.json),
[Frankfurt S3](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/eu-central-1/index.json),
[Frankfurt CloudWatch](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonCloudWatch/current/eu-central-1/index.json).

## Build and inspect locally

```sh
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/python scripts/package_lambda.py
terraform fmt -check -recursive deploy
terraform -chdir=deploy init -backend=false -input=false -lockfile=readonly
terraform -chdir=deploy validate
terraform -chdir=deploy test
```

Tested with Terraform 1.16.1; the module requires 1.16 or later in the 1.x line.
The committed provider lock file pins AWS provider 6.70.0. Native Terraform tests
use a **mocked AWS provider and plan operations only**. They do not use credentials,
call AWS or create resources. They check paper mode, private/CAS-protected state,
IAM scope, scheduler trust, retry limits, opt-in scheduling and stopping the worker.
Provider downloads require internet access. There is no deployment step in CI.

Packaging uses an allowlist of application Python sources and the paper config;
it never zips the repository root, `.env`, local state, `.venv` or `.aws`.
The paper worker uses the managed Python Lambda runtime's boto3/botocore. Locally,
conditional PutObject support is checked with the locked SDK and botocore Stubber.
No native macOS wheel is shipped. A managed-runtime smoke test is required after
reviewed provisioning; if SDK behavior changes, pin/bundle only that SDK in a new
reviewed package. The MCP/Bedrock connectors are not invoked by this deployment.

## Select the target and Terraform state

Before a real AWS plan or apply, choose the profile, exact account ID, region,
monthly budget and alert route. Copy `terraform.tfvars.example` to
`terraform.tfvars` in this directory and replace its synthetic values. The provider's
`allowed_account_ids` rejects credentials for a different account. Credentials
come from your normal AWS login/profile; never put keys in variables or chat.

Terraform state tracks provisioned resources. It is separate from the application's
`paper/state.json` investment ledger. Keep both private and backed up. Terraform
defaults to local state. For an initial single-maintainer paper deployment, keep
`deploy/terraform.tfstate` and its backups private and backed up; losing this file
loses Terraform's resource mappings, while the application ledger remains in S3.
Use a durable private backend for shared or unattended infrastructure management.
To use an **existing, separately approved** private S3 state bucket:

1. Copy `backend.tf.example` to `backend.tf` and `backend.hcl.example` to
   `private.backend.hcl` inside `deploy/`.
2. Choose the state bucket, region and profile. Keep the dedicated
   `etf-autopilot-paper/terraform.tfstate` key; do not reuse another project's state.
3. Confirm the bucket has public access blocked, encryption, versioning and scoped
   access. The backend identity needs access to this state and its `.tflock` object.
4. Initialize with the command below. If Terraform detects pre-existing local state,
   stop and review any state migration before proceeding.

```sh
terraform -chdir=deploy init -input=false -lockfile=readonly \
  -backend-config=private.backend.hcl
```

The optional backend bucket is not created by this module; its selection/bootstrap
is a separate account setup task. `.gitignore`, the privacy hooks and the source-package
allowlist exclude real variable/backend files, provider caches, state and plans.
The provider lock file and placeholder examples are public. Do not upload a raw
workspace archive or put a backend file with credentials into a release package.

Sources: [S3 backend and locking](https://developer.hashicorp.com/terraform/language/backend/s3),
[provider mocking](https://developer.hashicorp.com/terraform/language/tests/mocking),
[Scheduler trust boundary](https://docs.aws.amazon.com/scheduler/latest/UserGuide/cross-service-confused-deputy-prevention.html).

## Review a deployment plan

Only after account/region/budget and the state arrangement are selected:

```sh
.venv/bin/python scripts/package_lambda.py
terraform -chdir=deploy plan -input=false -out=review.tfplan
terraform -chdir=deploy show -no-color review.tfplan
```

Review every proposed resource. They must belong only to this root, with paper mode
and scheduling disabled. A plan file can contain sensitive data; keep it local and
ignored. After the owner approves that exact plan, the deployment command is:

```sh
terraform -chdir=deploy apply review.tfplan
```

Offline checks do not run real account plans or applies. Account-specific plans
remain local and require review before paid provisioning. There is no VPC, NAT or
SAM artifact bucket. The application state bucket and log group have `prevent_destroy`;
the bucket also refuses forced emptying. Old noncurrent ledger versions expire after
30 days while the current object retains the cumulative journal until its size cap.
Keep separate archival backups before migration or resource removal.

## One finite paper run and verification

After approved deployment, obtain the exact names with
`terraform -chdir=deploy output -raw worker_name` and
`terraform -chdir=deploy output -raw state_bucket_name`.
Extract one event with Python, without keys:

```sh
.venv/bin/python - <<'PY'
import json
from pathlib import Path
rows = json.loads(Path('fixtures/monthly_events.json').read_text())
Path('var/aws-paper-input.json').write_text(json.dumps(rows[0]))
PY
aws s3 cp var/aws-paper-input.json s3://YOUR_STATE_BUCKET/paper/inputs/current.json \
  --profile YOUR_PROFILE --region YOUR_REGION
aws lambda invoke --function-name YOUR_WORKER_NAME --payload '{"action":"run"}' \
  --cli-binary-format raw-in-base64-out --profile YOUR_PROFILE --region YOUR_REGION \
  var/aws-result.json
```

Inspect the result file for `status: completed`; a CLI exit code alone does not
prove that application execution succeeded. Invoke the **same** event again;
verify `duplicate: true` and unchanged ledger fills/fees. Invoke
`{"action":"reconcile"}` and `{"action":"health"}` and inspect their JSON results.
Download the current state for local read-only inspection if needed; do not overwrite
it by hand. Verify log delivery, retained state and the failure queue. No real
broker call or model invocation is part of this smoke test.

Use subsequent fixture rows with their stable IDs to advance the monthly demo.
Do not repeatedly generate new IDs for the same decision. An unknown S3 write
requires checking persisted state and replaying the same event; no side effect
outside simulated state is allowed in a CAS transition.

## Start, stop and remove

There is no continuously running worker. The daily health schedule starts disabled.
Only after verification, set `enable_health_schedule = true` in a reviewed Terraform
update. To stop scheduled runs, set it back to `false`. Set
`worker_reserved_concurrency = 0` to stop manual invocation as well. Review a fresh
plan before applying either update. Neither operation erases state or sells assets.

For local Terraform state, update its private backup after each approved apply.
One option is the infrastructure-only key inside this application's encrypted,
versioned bucket; the worker's read permissions cover only its paper input/ledger:

```sh
aws s3 cp deploy/terraform.tfstate \
  s3://YOUR_STATE_BUCKET/infrastructure/terraform-state-backup.json \
  --sse AES256 --profile YOUR_PROFILE --region YOUR_REGION
```

Verify the backup and keep a private local copy too. Use the optional S3 backend
for shared management so infrastructure updates coordinate through remote state
and locking. Never copy another application's state into this root.

Terraform intentionally blocks destroying the protected state bucket/log group
while their `prevent_destroy` rules remain in this configuration. Stopping execution
still leaves storage costs. Removal requires a separate reviewed plan, backups and
the owner's explicit deletion authorization. Never remove another project's
resources or use its Terraform state. No broad infrastructure changes are needed.

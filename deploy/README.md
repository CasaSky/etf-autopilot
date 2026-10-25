# AWS paper deployment — review before provisioning

**Prepared and statically validated; not deployed.** The required next choices are
AWS profile/account, region, monthly budget and alert route. No broker credentials,
permanent broker budget, model permissions or money are configured by this template.
Use only this repository and a separately named `etf-autopilot-paper` stack.

## Resources and limits

AWS SAM/CloudFormation creates one Python 3.13 Lambda, a private AES256-encrypted,
versioned S3 bucket, an SQS failure queue, seven-day CloudWatch logs, scoped IAM
roles and a **disabled** daily EventBridge Scheduler health check. The worker has
256 MB, a 30-second timeout and reserved concurrency one. Async Lambda and
Scheduler automatic retries are zero. S3 SDK attempts are explicitly one.

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

## Reviewable cost estimate

Planning estimate, 24 October 2026; recheck selected-region rates before deployment.
This deliberately assumes no free-tier credit. Illustrative conservative USD unit
rates below are **budget assumptions, not verified Frankfurt price quotes**.

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

## Build and inspect locally

```sh
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/cfn-lint deploy/template.yaml
.venv/bin/python scripts/package_lambda.py
```

Packaging uses an allowlist of application Python sources and the paper config;
it never zips the repository root, `.env`, local state, `.venv` or `.aws`.
The paper worker uses the managed Python Lambda runtime's boto3/botocore. Locally,
conditional PutObject support is checked with the locked SDK and botocore Stubber.
No native macOS wheel is shipped. A managed-runtime smoke test is required after
reviewed provisioning; if SDK behavior changes, pin/bundle only that SDK in a new
reviewed package. The MCP/Bedrock connectors are not invoked by this deployment.

AWS SAM CLI is a separate prerequisite, not installed or invoked by this delivery.
Review the packaged CloudFormation change set before execution. The following
commands are for the user-authorized deployment stage, **not an instruction to
provision before account/region/budget selection**:

```sh
sam deploy --template-file deploy/template.yaml --stack-name etf-autopilot-paper \
  --resolve-s3 --capabilities CAPABILITY_IAM \
  --region YOUR_REGION --profile YOUR_PROFILE --confirm-changeset \
  --parameter-overrides ScheduleState=DISABLED
```

Confirm the change set contains only this stack's resources. No Terraform or
sibling application resources are involved. `sam --resolve-s3` also uses a managed
artifact bucket; review its storage/lifecycle. The private state bucket and log
group are retained on stack deletion. Old noncurrent state versions expire after
30 days; current state retains the cumulative journal until the size cap. Keep
separate archival backups before any migration or resource removal.

## One finite paper run and verification

After approved deployment, obtain the exact WorkerName and StateBucketName from
this stack's CloudFormation outputs. Extract one event with Python, without keys:

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
Only after verification, enable the `ScheduleState` parameter in a reviewed update.
To stop scheduled runs, set it back to `DISABLED`; to prevent manual execution as
well, set the worker's reserved concurrency to zero using its exact function name.
Neither operation erases state or sells assets.

A future removal uses a change set for **this stack only**. Retained S3/log resources
continue to exist and can incur storage costs. Back up audit history and ask the
user before permanent data/resource deletion. Never remove another project's
resources or apply broad infrastructure changes.

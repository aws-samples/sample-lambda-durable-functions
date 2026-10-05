# Ephemeral GitHub self-hosted runners on AWS Lambda MicroVMs

A SAM stack that runs **ephemeral GitHub Actions self-hosted runners** inside
**AWS Lambda MicroVMs** (Firecracker-isolated, snapshot-resumable), orchestrated
by a **Lambda durable function** using the **callback pattern**, fronted by
**API Gateway**, and configured through **Parameter Store**.

## Architecture

```
GitHub webhook (workflow_job: queued)
        │
        ▼
  API Gateway  POST /dispatch ──► Dispatch Lambda ──(async invoke)──► Durable orchestrator (alias)
       (verifies X-Hub-Signature-256,                                       │
        mints a JIT runner config via the GitHub App)                       │
                            step  read-config        (SSM: image ARN/version, exec role, timeout, port)
                            create_callback          mint the callbackId up front
                            step  run-microvm         RunMicrovm  ──► boots one runner VM; runHookPayload
                                                      carries only {callbackId, callbackUrl} (tiny, <4096)
                            step  create-auth-token   CreateMicrovmAuthToken (60-min, port-scoped)
                            step  configure-runner    POST /configure to the VM with the JIT config
                                                      (ingress has no size limit; retries the brief 502
                                                       the proxy returns during snapshot restore)
                            callback.result()
                                 ──►  SUSPENDS  (zero compute cost while the job runs)
                                            ▲
  runner finishes its one job             │
        │                                  │
        ▼                                  │
  API Gateway  POST /callback ─► Callback Lambda ─► SendDurableExecutionCallbackSuccess/Failure
                                                                           │
                            (orchestrator resumes)                         ▼
                            step  terminate-microvm   TerminateMicrovm  (ephemeral teardown)
```

Because the durable function **suspends** at the callback, you pay no compute for
the orchestrator while the CI job runs — whether that's 2 minutes or an hour. The
runner VM is **ephemeral**: one job per VM, torn down on callback.

**Why the JIT config travels over `/configure`, not `runHookPayload`:** a real
GitHub `encoded_jit_config` is ~4 KB, and `runHookPayload` is capped at 4096
characters — it does not fit reliably, even gzipped. The MicroVM's ingress
channel has no such cap, so the large config is delivered to the running VM via
`/configure`, while only the tiny `{callbackId, callbackUrl}` rides in
`runHookPayload` (delivered to the `/run` hook). The proxy can return a 502 for
the first few seconds after `RunMicrovm` while the snapshot restores, so the
`configure-runner` step retries with backoff.

## Why the MicroVM image is a custom resource

AWS Lambda MicroVMs are managed by the `lambda-microvms` API, **not** by native
CloudFormation resource types — there is no `AWS::LambdaMicrovms::*`. So the
MicroVM **image** is created in-stack by a CloudFormation **custom resource**
(`MicrovmImage`) backed by `functions/image_resource`, which:

- on **Create/Update** → calls `CreateMicrovmImage` (S3 code artifact + base
  image + build role), waits for an `ACTIVE` version, and returns
  `ImageArn` / `ImageVersion`;
- on **Delete** → calls `DeleteMicrovmImage`.

Those values are written to Parameter Store (`ImageArnParam`,
`ImageVersionParam`) and read at runtime by the orchestrator.

## Layout

```
template.yaml                     SAM stack (API GW, durable Lambda, custom resource, SSM, IAM)
functions/
  orchestrator/handler.py         durable function: run → configure → callback → terminate
  orchestrator/microvm_api.py     SigV4 REST client for the lambda-microvms API (see note below)
  callback/handler.py             POST /callback → SendDurableExecutionCallbackSuccess/Failure
  dispatch/handler.py             POST /dispatch (GitHub webhook) → verify + mint JIT → start execution
  dispatch/requirements.txt       PyJWT[crypto] for the GitHub App RS256 JWT
  image_resource/handler.py       custom resource: CreateMicrovmImage / DeleteMicrovmImage
  image_resource/microvm_api.py   same SigV4 REST client (bundled per function)
layers/durable_sdk/               durable execution SDK, built into a layer by `sam build`
runner-app/                       sample MicroVM app: Dockerfile + lifecycle-hook HTTP server
examples/microvm-test.yml         sample GitHub Actions workflow targeting the runner
```

> **No environment-specific configuration lives in this repo.** There are no
> account IDs, region literals (beyond a safe `us-east-1` fallback used only if
> `AWS_REGION` is unset), API URLs, bucket names, secrets, or GitHub App/org IDs
> in the source. Everything environment-specific is supplied at deploy time via
> SAM parameters (`CodeArtifactBucket`, etc.) or stored by you in SSM
> SecureString parameters (webhook secret, GitHub App credentials). The MicroVMs
> API is reached through a small SigV4 REST client (`microvm_api.py`) because the
> `lambda-microvms` API is not yet in the public boto3/botocore shipped in the
> Lambda runtime — swap it for a boto3 client once botocore includes the model.

## Prerequisites

- AWS CLI + SAM CLI, credentials for a **non-production** account first.
- Lambda MicroVMs available in your target region
  (`aws lambda-microvms list-managed-microvm-images`).
- An **S3 bucket in the same region** to hold the runner app zip.

## Deploy

### 1. Package and upload the runner app

The MicroVM image is built from a zip with a `Dockerfile` at its root.

```bash
cd runner-app
zip -r ../runner-app.zip Dockerfile app.py entrypoint.sh
cd ..
aws s3 cp runner-app.zip s3://<YOUR_BUCKET>/runner-app.zip
```

### 2. Build and deploy the stack

```bash
sam build --use-container   # builds the durable SDK layer + dispatch deps in a
                            # Lambda-matching container (dispatch needs the
                            # cryptography native wheel for PyJWT RS256 signing)
sam deploy --guided \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      CodeArtifactBucket=<YOUR_BUCKET> \
      CodeArtifactKey=runner-app.zip
```

> `--use-container` is recommended here because the dispatch Lambda depends on
> `cryptography` (a compiled wheel) — building in the container ensures the right
> manylinux/arch wheel for the Lambda runtime.

#### Building with Finch instead of Docker

AWS SAM CLI supports **Finch** as the container engine behind `--use-container`
natively (SAM CLI ≥ 1.145.0; `sam --version` to check). No `DOCKER_HOST` tricks
needed — SAM detects it.

```bash
# macOS (first time): install + start the Finch VM
brew install finch
finch vm init && finch vm start

# Linux: install Finch, then `sudo systemctl start finch` (or the generic installer)
```

SAM treats Finch as a **fallback to Docker** — if the Docker daemon is also
running, SAM uses Docker. To force Finch while Docker is installed, either stop
Docker, or set the admin preference (macOS):

```bash
sudo /usr/libexec/PlistBuddy -c "Add :DefaultContainerRuntime string finch" \
  /Library/Preferences/com.amazon.samcli.plist
```

Then build as normal — SAM runs the containerized build on Finch:

```bash
sam build --use-container
```

Caveats (from the SAM/Finch docs):
- **macOS directory mounting:** keep the project under `~` or `/Volumes`, or add
  its path to `additional_directories` in `~/.finch/finch.yaml` and
  `finch vm stop && finch vm start`.
- **Cross-arch builds:** this stack targets `arm64`. If your host isn't arm64,
  enable emulation once:
  `sudo finch run --privileged --rm tonistiigi/binfmt:master --install all`.

On create, the `MicrovmImage` custom resource runs `CreateMicrovmImage` and
**waits for the build to finish** (can take several minutes). Leave the base
image blank to auto-resolve the newest managed AL2023 base, or pass
`BaseImageArn=arn:aws:lambda:<region>:aws:microvm-image:al2023-1`.

Stack outputs include `DispatchUrl`, `CallbackUrl`, `MicrovmImageArn`, and
`OrchestratorAliasArn`.

### 3. Create the webhook secret (SecureString) — before pointing GitHub at it

CloudFormation can't create `SecureString` parameters, so you create it yourself
and the stack references it by name. Generate a strong secret, store it, and use
the **same** value in the GitHub webhook config.

```bash
SECRET=$(openssl rand -hex 32)
aws ssm put-parameter --type SecureString \
  --name /gh-microvm-runners/github/webhook-secret \
  --value "$SECRET"
echo "$SECRET"   # paste this into GitHub's webhook 'Secret' field
```

If you use a non-default parameter path, pass it at deploy with
`--parameter-overrides WebhookSecretParamName=/your/path`. The dispatch Lambda
reads it (decrypted) and verifies `X-Hub-Signature-256` on every delivery.

### 4. Create the GitHub App credentials (SecureString) — for JIT minting

The dispatch Lambda mints a just-in-time runner config per job by calling
GitHub's `generate-jitconfig`, which needs a **GitHub App** (App ID,
installation ID, private key). Store those as a SecureString the stack
references by name (default `/gh-microvm-runners/github/app-credentials`), as a
JSON object:

```bash
# PEM newlines must be JSON-escaped as \n — jq -Rs does this for you.
PRIVATE_KEY=$(jq -Rs . < app-private-key.pem)
aws ssm put-parameter --type SecureString \
  --name /gh-microvm-runners/github/app-credentials \
  --value "{\"appId\":\"123456\",\"installationId\":\"12345678\",\"privateKey\":${PRIVATE_KEY}}"
```

Create a GitHub App with **Actions: read & write** and **Administration:
read & write** (self-hosted runner) permissions, install it on your org/repo,
and use its App ID + installation ID above. Override the parameter path or the
runner group with `--parameter-overrides AppCredentialsParamName=/your/path
RunnerGroupId=1 RequiredRunnerLabel=lambda-microvms`.

**Placeholder behavior:** until this parameter exists and is populated, dispatch
skips minting and sends an empty `jitConfig` — the pipeline still runs end to
end (the runner reports `NoJitConfig`), so you can deploy and test the plumbing
before wiring the App.

### 5. Point GitHub at the dispatcher

Add a repo/org webhook for the **workflow_job** event targeting the `DispatchUrl`
output, content type `application/json`, and the **Secret** set to the value
above. The dispatcher rejects any delivery whose HMAC signature doesn't match
(HTTP 401), ignores everything except `action = queued`, and only mints a runner
for jobs carrying the required label (default `lambda-microvms`).

#### How verification works

```
GitHub ──(payload + X-Hub-Signature-256)──► API GW /dispatch ──► Dispatch Lambda
                                                                     │
                        1. read secret from SSM (SecureString, decrypted + cached)
                        2. HMAC-SHA256 over the RAW body, keyed on the secret
                        3. constant-time compare to the sha256=... header
                        4. 401 on mismatch; only then async-invoke the orchestrator
```

Verification happens in **dispatch**, not the orchestrator: dispatch is the only
function that sees the raw body and the signature header. The orchestrator is
invoked with a clean JSON payload after the request is already trusted.

## The runner image

`runner-app/` is a working **no-docker** runner image, built from `ubuntu:24.04`:

- `Dockerfile` installs the real **GitHub Actions runner** (arm64, pinned
  version), the AWS CLI v2, Python 3, and git/jq.
- `app.py` is the lifecycle-hook server (serving the
  `/aws/lambda-microvms/runtime/v1/*` hook paths on port 8080). The `/run` hook
  captures the `{callbackId, callbackUrl}` from `runHookPayload`; the
  orchestrator then delivers the **JIT config** to `/configure`, where `app.py`
  decodes it — accepting either the plain base64 `encoded_jit_config` or
  `base64(gzip(encoded_jit_config))` (gzip magic-byte sniff) — derives the runner
  name from the nested `.runner` struct for logging, and starts the single job.
- `entrypoint.sh` runs the runner once: `./run.sh --jitconfig "$ENCODED_JIT_CONFIG"`.
  JIT registration is inherently single-job: the runner registers, runs one job,
  auto-deregisters, and exits.
- On exit, `app.py` POSTs the result to `callbackUrl`, resuming the suspended
  durable function, which then calls `TerminateMicrovm`.

### How the JIT config is minted and threaded

The runner **consumes** a JIT config; the dispatch Lambda **mints** it. The full
chain is implemented — you only supply the GitHub App credentials (step 4):

```
dispatch handler  (functions/dispatch/handler.py)
   App JWT (RS256) ─► installation token ─► generate-jitconfig
   └─ puts encoded_jit_config on the orchestrator event (github.jitConfig)
      │
      ▼
orchestrator  (functions/orchestrator/handler.py)
   runHookPayload = {callbackId, callbackUrl}   (tiny; fits the 4096 cap)
   configure-runner step  ─► POST /configure {jitConfig}   (ingress, no size cap,
                              retried through the brief snapshot-restore 502)
      │
      ▼
runner /configure  decodes jitConfig → entrypoint.sh → run.sh --jitconfig
```

Dispatch authenticates as the GitHub App (JWT signed with the App private key),
exchanges it for a short-lived installation token, calls `generate-jitconfig`
(repo endpoint when the webhook carries `repository.full_name`, else the org
endpoint), and puts the returned `encoded_jit_config` on the job. RS256 signing
uses `PyJWT[crypto]`, declared in `functions/dispatch/requirements.txt` and
installed by `sam build`.

Credentials/token values are never logged — error paths log only the exception
type, since a parse/HTTP error can embed token fragments.

## Lifecycle hooks (why they're enabled)

The image is built with hooks enabled (`functions/image_resource/handler.py`):

- `/ready` (build) — snapshot only after the app has fully booted.
- `/validate` (build) — confirm the snapshot and let the platform prefetch the
  hot path, cutting cold-start latency on every `RunMicrovm`.
- `/run`, `/suspend`, `/resume`, `/terminate` (runtime) — fast (1–60 s)
  notifications; here `/run` captures the GitHub registration details.

## Validate locally

```bash
cfn-lint template.yaml
sam validate --template template.yaml
```

> The orchestrator carries a scoped `cfn-lint` suppression for `E3002` on
> `DurableConfig`: it is a real, documented `AWS::Lambda::Function` property that
> older cfn-lint resource specs don't recognize yet.

## Cleanup

`sam delete` removes the stack. Deleting the `MicrovmImage` resource calls
`DeleteMicrovmImage`. MicroVM image **versions incur storage cost even when no
VMs run on them**, so confirm the image is gone after teardown. Durable function
deletion waits for in-flight executions to finish (CloudFormation waits up to
1 hour), so terminate or let any pending runner jobs complete first.

## Key constraints baked into this design

- **MicroVMs aren't CloudFormation-native** → custom resource for the image.
- **Durable execution can't be retrofitted** → the orchestrator is created with
  `DurableConfig` from the start and invoked via its **alias** (qualified ARN).
- **Determinism** → every AWS API call is a durable `step`; nothing
  non-deterministic runs outside a step.
- **One VM per job** → ephemeral runner model; terminate on callback.
- **Auth token TTL ≤ 60 min** → refresh if you extend jobs beyond that.


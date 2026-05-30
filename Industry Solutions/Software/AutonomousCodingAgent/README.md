# Autonomous Coding Agent — Durable Functions + AgentCore

> **This is a simplified demo.** For a production-grade solution, see [ABCA (Autonomous Background Coding Agents)](https://github.com/aws-samples/sample-autonomous-cloud-coding-agents) — an open-source reference architecture with memory, guardrails, concurrency control, and multi-repo support. Evaluate it for your own use case and ensure appropriate guardrails are in place.

## The Problem

Engineers spend hours on routine code tasks — addressing PR feedback, fixing lint issues, implementing well-spec'd features — while you pay for their attention and context-switching costs.

## What This Demo Shows

You submit a coding task, walk away, and come back to generated code (or a PR). The agent clones the repo, writes code using Amazon Bedrock, and delivers the result autonomously in an isolated cloud environment. No babysitting, no IDE sessions, no back-and-forth.

**Why it matters:**
- **Reclaim engineer time** — routine work runs in the background while humans focus on design and decisions
- **Faster cycle time** — tasks execute 24/7, no queue behind a human's calendar
- **Cost-controlled** — the durable orchestrator suspends between polls (zero compute cost during waits)

## Architecture

```
Lambda Durable Function (orchestrator)
  ├─ step("validate")           ← checkpointed, won't re-run on replay
  ├─ step("start-session")      ← InvokeAgentRuntime → starts container
  ├─ waitForCondition("poll")   ← suspends 30s between checks (no cost)
  └─ step("finalize")           ← returns result
                    │
                    ▼
        AgentCore Runtime (isolated container)
          ├─ Clone repo
          ├─ Bedrock Converse (Claude Sonnet 4) → generate code
          ├─ git commit + push
          └─ Create GitHub PR (or write to S3 for testing)
```

## Run the Demo (Step by Step)

### Prerequisites

- AWS account with Bedrock model access (Claude Sonnet 4)
- SAM CLI, Node.js 22+, Finch or Docker
- GitHub PAT with `repo` scope (for PR mode)

### Deploy (~5 minutes)

```bash
# Set your region and account
export REGION=us-east-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 1. Create ECR repo and push agent container (arm64 required)
aws ecr create-repository --repository-name coding-agent --region $REGION
aws ecr get-login-password --region $REGION | finch login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com
cd agent
finch build --platform linux/arm64 -t coding-agent .
finch tag coding-agent:latest ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/coding-agent:latest
finch push ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/coding-agent:latest
cd ..

# 2. Build orchestrator
cd orchestrator && npm install && npm run build && cd ..

# 3. Deploy stack
cd infra
sam deploy --stack-name durable-coding-agent --region $REGION \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides "Region=${REGION} ECRRepoName=coding-agent" \
  --resolve-s3 --no-confirm-changeset

# 4. Create S3 output bucket (for testing without GitHub)
aws s3 mb s3://durable-coding-agent-output-${ACCOUNT_ID} --region $REGION
```

### Test Code Generation (S3 mode — no GitHub needed)

```bash
aws lambda invoke \
  --function-name durable-coding-agent \
  --qualifier '$LATEST' \
  --invocation-type Event \
  --region $REGION \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "repo": "example/my-app",
    "task_description": "Create a Python Flask app with a GET /health endpoint that returns JSON with status and UTC timestamp",
    "output_bucket": "durable-coding-agent-output-'${ACCOUNT_ID}'"
  }' /dev/null

# Wait ~40s, then check output
aws s3 ls s3://durable-coding-agent-output-${ACCOUNT_ID}/coding-agent/ --recursive
aws s3 cp s3://durable-coding-agent-output-${ACCOUNT_ID}/coding-agent/<task-id>/app.py -
```

### GitHub PR Mode

```bash
# Store your GitHub token
aws secretsmanager put-secret-value \
  --secret-id durable-coding-agent/github-token \
  --secret-string 'ghp_YOUR_TOKEN' \
  --region $REGION

# Submit a task — agent will clone, code, and open a PR
aws lambda invoke \
  --function-name durable-coding-agent \
  --qualifier '$LATEST' \
  --invocation-type Event \
  --region $REGION \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "repo": "your-org/your-repo",
    "task_description": "Add a /health endpoint following the pattern in routes/users.ts"
  }' /dev/null
```

### Demo UI (local browser)

The project includes a single-file browser UI (`demo.html`) with a file explorer view. No build step, no npm install — just serve it locally.

```bash
# Serve the UI (needed for CORS — can't use file://)
python3 -m http.server 8080

# Open in browser
open http://localhost:8080/demo.html
```

Paste the API Gateway URL (from the SAM deploy output) into the API field, type a task, and click **Generate**. The UI polls for results and displays generated files in a VS Code-style tree + editor layout.

The API URL is in the stack outputs:
```bash
aws cloudformation describe-stacks --stack-name durable-coding-agent \
  --query "Stacks[0].Outputs[?OutputKey=='DemoApiUrl'].OutputValue" --output text
```

## Example Prompts for Demo Presenters

These represent the sweet spot — well-defined, bounded work you can describe in a sentence and walk away from. Each maps to a source system that would trigger the task automatically in production.

### 🌙 Backlog overnight
*"Point it at GitHub issues you've already labeled and let it churn through them while you sleep."*

A cron job scans for issues tagged `good-first-issue` or `tech-debt`, submits each one to the agent, and you wake up to a stack of PRs to review instead of a stack of tickets to write. The demo prompt adds type hints across a module — tedious, well-specified, zero ambiguity.

### 📊 Coverage bot
*"Write unit tests for this module. The outcome is measurable and it rarely touches production code paths."*

Your CI pipeline detects coverage dropped below threshold and fires a task: "write tests for PaymentService." The agent produces tests, you review. Safe way to put agents to work because the blast radius is test files only.

### 🔄 Dep upgrade CI
*"Migrating off a deprecated API, bumping dependencies — tedious, well-specified, and low-risk."*

Dependabot tells you `requests` is deprecated. Instead of a human spending an afternoon on a mechanical find-and-replace, the agent migrates to `httpx`, preserves error handling, and opens a PR. You review the diff, not write it.

### 💬 Slack request
*"Add a new endpoint following the pattern in users.ts — pattern-matching work where the codebase already shows the agent what good looks like."*

An engineer types in Slack: "need an /orders endpoint like /users." The webhook fires, the agent clones the repo, sees the pattern, replicates it with the new resource, and opens a PR. No context-switching for the senior engineer who owns that service.

### 🐛 Error tracker
*"Wire it so a bug report automatically spawns a fix attempt. Fire-and-forget: it either opens a plausible PR or it doesn't."*

Sentry detects a spike in dropped webhook events. The alert triggers a task describing the symptom. The agent reads the code, identifies the missing dead-letter queue, and opens a PR with the fix. You review — worst case, you close the PR and fix it yourself with the agent's attempt as a starting point.

### 📡 Fan-out patch
*"A single change you need everywhere — dispatched across all repos in parallel rather than one at a time."*

The platform team decides: "all services move to Node 22." Instead of 15 PRs opened manually across 15 repos, one dispatch fans out the same task to every service's blueprint. Fifteen PRs land in parallel. You review and merge.

## What's Happening Under the Hood

1. **You invoke the Lambda** (async, since durable executions can run for hours)
2. **Orchestrator validates** and starts an AgentCore session (checkpointed)
3. **AgentCore spins up** the container, delivers the task via `POST /invocations`
4. **Agent runs the pipeline** — clone, Bedrock inference, commit, push, PR
5. **Orchestrator polls** every 30s via `waitForCondition` — suspends between polls (you pay nothing)
6. **Agent signals done** — orchestrator finalizes and returns the result

The durable function can run for up to **1 year**. If the Lambda crashes mid-execution, it replays from the last checkpoint automatically.

## Project Structure

```
AutonomousCodingAgent/
├── orchestrator/src/index.ts    # Durable Function: validate → start → poll → finalize
├── agent/src/server.py          # AgentCore: /ping + /invocations
├── agent/src/pipeline.py        # Clone → Bedrock → commit → PR (or S3)
├── agent/Dockerfile             # arm64 Python container
├── infra/template.yaml          # SAM: Lambda + AgentCore + IAM
└── README.md
```

## For Production Use

This demo is intentionally minimal (~300 lines). For production workloads, [ABCA](https://github.com/aws-samples/sample-autonomous-cloud-coding-agents) adds:

- **Memory** — agents learn from past PR feedback and improve over time
- **Guardrails** — Bedrock Guardrails screen prompts, Cedar policies gate tool use
- **Concurrency control** — per-user limits, system-wide caps, cost budgets per task
- **Multi-repo blueprints** — fan-out the same change across many repos in parallel
- **Observability** — full task lifecycle events, CloudWatch dashboards, trace artifacts
- **Multiple input channels** — Slack, CLI, webhooks, GitHub issue integration

## Technologies

- AWS Lambda Durable Functions (`@aws/durable-execution-sdk-js`)
- Amazon Bedrock AgentCore Runtime
- Amazon Bedrock (Claude Sonnet 4)
- AWS SAM
- TypeScript / Python

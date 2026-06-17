# Durable Coding Agent

An autonomous coding agent built with **AWS Lambda Durable Functions** (orchestrator) and **Amazon Bedrock AgentCore Runtime** (agent). Submit a coding task, and the agent clones a repo, generates code with Bedrock, pushes a branch, and the orchestrator opens a pull request — all running serverlessly with zero idle cost.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        User / API Gateway                        │
└──────────────────────────────┬──────────────────────────────────┘
                               │ POST /tasks { repo, task }
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│              Lambda Durable Function (Orchestrator)               │
│                                                                   │
│  1. context.step("validate") → validate input                    │
│  2. context.waitForCallback("agent-execution")                   │
│       → InvokeAgentRuntime, pass callbackId to agent             │
│       → SUSPEND (zero compute cost until callback received)      │
│  3. context.step("parse-agent-result") → parse callback payload  │
│  4. context.step("create-pull-request") → GitHub API             │
│       → Fetch token from Secrets Manager, create PR              │
│                                                                   │
│  Suspends during agent execution. No polling. No compute cost.   │
└──────────────────────────────┬──────────────────────────────────┘
                               │ InvokeAgentRuntime
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│              AgentCore Runtime (Container on ECR)                 │
│                                                                   │
│  FastAPI server (port 8080):                                     │
│    GET  /ping         → health check (healthy | HealthyBusy)     │
│    POST /invocations  → accept task, spawn background thread     │
│                                                                   │
│  Background pipeline:                                            │
│    1. Clone repo (token from Secrets Manager)                    │
│    2. Invoke Bedrock model (Claude) to generate code             │
│    3. Apply changes, commit, push branch                         │
│    4. SendDurableExecutionCallbackSuccess(CallbackId, Result)    │
│       → Resumes the orchestrator with {branch_name, files}       │
└─────────────────────────────────────────────────────────────────┘
```

## How It Works

1. **Submit a task** — invoke the Lambda with `{ repo, task_description }`.
2. **Orchestrator starts** — validates input, calls `InvokeAgentRuntime` passing a `callbackId`, then **suspends** (zero cost).
3. **Agent runs** — clones the repo, uses Claude on Bedrock to generate code, commits, pushes the branch.
4. **Agent sends callback** — calls `SendDurableExecutionCallbackSuccess` with the branch name and file list, which **resumes** the orchestrator.
5. **Orchestrator creates PR** — fetches the GitHub token from Secrets Manager and creates a pull request via the GitHub API.

### Two output modes

- **GitHub mode** (default): Agent pushes a branch, orchestrator opens a PR.
- **S3 mode**: If `output_bucket` is provided, generated files are written to S3 instead. No GitHub interaction needed.

## Key Concepts

| Concept | What it does |
|---------|-------------|
| `context.waitForCallback()` | Suspends the function (no charges) until an external signal arrives via `SendDurableExecutionCallbackSuccess`. |
| `context.step()` | Checkpointed unit of work. Replayed on restart without re-executing. |
| `SendDurableExecutionCallbackSuccess` | Lambda API called by the agent to resume the orchestrator with a result payload. |
| `DurableConfig` | SAM property enabling durable execution on a Lambda function. |
| AgentCore Runtime | Managed container hosting with `/ping` + `/invocations` contract. |
| `HealthyBusy` | Tells AgentCore "I'm working, don't idle-terminate me." |

## Project Structure

```
AutonomousCodingAgent/
├── orchestrator/              # Lambda Durable Function (TypeScript)
│   ├── src/index.ts           # Orchestrator: validate → waitForCallback → create PR
│   ├── src/secrets-manager.d.ts
│   ├── package.json
│   └── tsconfig.json
├── agent/                     # AgentCore Runtime (Python)
│   ├── src/
│   │   ├── server.py          # FastAPI /ping + /invocations
│   │   └── pipeline.py        # Clone → generate → commit → push → callback
│   ├── Dockerfile
│   └── requirements.txt
├── infra/
│   └── template.yaml          # SAM template (Lambda + AgentCore + IAM + API GW)
├── deploy.sh                  # One-command deployment script
├── test_local.py              # Local testing helper
└── demo.html                  # Simple browser UI
```

## Deployment

### Prerequisites

- AWS CLI configured
- SAM CLI installed
- Finch or Docker (for building the agent container)
- A GitHub Fine-Grained Personal Access Token with **Contents** (read/write) and **Pull requests** (read/write) permissions

### One-Command Deploy

```bash
./deploy.sh
```

This will:
1. Create the ECR repository
2. Build and push the agent container (arm64)
3. Build the orchestrator
4. Create the GitHub token secret in Secrets Manager
5. Deploy the SAM stack

### Store your GitHub token

```bash
aws secretsmanager put-secret-value \
  --secret-id durable-coding-agent/github-token \
  --secret-string 'github_pat_YOUR_TOKEN' \
  --region us-east-1
```

### Invoke

```bash
# GitHub mode — generates code and opens a PR
aws lambda invoke \
  --function-name durable-coding-agent \
  --qualifier '$LATEST' \
  --cli-binary-format raw-in-base64-out \
  --invocation-type Event \
  --payload '{
    "repo": "owner/repo-name",
    "task_description": "Add a health check endpoint"
  }' \
  /dev/null

# S3 mode — writes generated files to S3 for inspection
aws lambda invoke \
  --function-name durable-coding-agent \
  --qualifier '$LATEST' \
  --cli-binary-format raw-in-base64-out \
  --invocation-type Event \
  --payload '{
    "repo": "owner/repo-name",
    "task_description": "Add a health check endpoint",
    "output_bucket": "my-output-bucket"
  }' \
  /dev/null
```

> **Note**: Durable functions require the `--qualifier '$LATEST'` flag.

## References

- [AWS Lambda Durable Functions](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html)
- [Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime.html)
- [sample-lambda-durable-functions](https://github.com/aws-samples/sample-lambda-durable-functions)

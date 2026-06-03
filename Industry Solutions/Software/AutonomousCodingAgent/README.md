# Durable Coding Agent

A simplified autonomous coding agent built with **AWS Lambda Durable Functions** (orchestrator) and **Amazon Bedrock AgentCore Runtime** (agent). Submit a coding task, and the agent clones a repo, writes code, and opens a pull request on GitHub — all running serverlessly in the cloud.

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
│  1. Validate request                                             │
│  2. context.step("start-session") → InvokeAgentRuntime           │
│  3. context.waitForCondition("poll") → poll every 30s            │
│  4. context.step("finalize") → check GitHub for PR              │
│                                                                   │
│  Checkpoints at each step. Suspends during waits (no compute).   │
│  Can run for up to 1 year. Costs only active execution time.     │
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
│    1. Clone repo, create branch                                  │
│    2. Invoke Bedrock model (Claude) to write code                │
│    3. Commit changes, push branch                                │
│    4. Create pull request via GitHub API                          │
│    5. Write result to DynamoDB                                   │
└─────────────────────────────────────────────────────────────────┘
```

## How It Works

1. **Submit a task** — invoke the Lambda with `{ repo, task_description, github_token }`.
2. **Orchestrator starts** — the durable function validates the input, then calls `InvokeAgentRuntime` to start an AgentCore session.
3. **Agent runs** — inside an isolated container, the agent clones the repo, uses Claude to generate code changes, commits, pushes, and opens a PR.
4. **Orchestrator polls** — using `waitForCondition`, the orchestrator checks every 30s if the agent is done. It suspends between polls (zero compute cost).
5. **Finalization** — once the agent signals completion, the orchestrator reads the result (PR URL or error) and returns it.

## Key Concepts

| Concept | What it does |
|---------|-------------|
| `context.step()` | Checkpointed unit of work. Replayed on restart without re-executing. |
| `context.waitForCondition()` | Suspends the function (no charges) until a condition is met. |
| `DurableConfig` | SAM property enabling durable execution on a Lambda function. |
| AgentCore Runtime | Managed container hosting with `/ping` + `/invocations` contract. |
| `HealthyBusy` | Tells AgentCore "I'm working, don't idle-terminate me." |

## Project Structure

```
durable-coding-agent/
├── orchestrator/          # Lambda Durable Function (TypeScript)
│   ├── src/index.ts       # Orchestrator handler
│   └── package.json
├── agent/                 # AgentCore Runtime (Python)
│   ├── src/
│   │   ├── server.py      # FastAPI /ping + /invocations
│   │   └── pipeline.py    # Clone → code → commit → PR
│   ├── Dockerfile
│   └── requirements.txt
└── infra/
    └── template.yaml      # SAM template (Lambda + AgentCore + IAM)
```

## Deployment

### Prerequisites

- AWS CLI configured with appropriate permissions
- SAM CLI installed
- Docker (for building the agent container)
- A GitHub Personal Access Token with `repo` scope

### Steps

```bash
# 1. Build and push the agent container
cd agent
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com
docker build -t coding-agent .
docker tag coding-agent:latest <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com/coding-agent:latest
docker push <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com/coding-agent:latest

# 2. Build the orchestrator
cd ../orchestrator
npm install && npm run build

# 3. Deploy the stack
cd ../infra
sam build && sam deploy --guided
```

### Invoke

```bash
aws lambda invoke \
  --function-name durable-coding-agent \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "repo": "owner/repo-name",
    "task_description": "Add a health check endpoint to the Express app",
    "github_token": "ghp_..."
  }' \
  response.json
```

## References

- [AWS Durable Functions Docs](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html)
- [Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime.html)
- [sample-lambda-durable-functions](https://github.com/aws-samples/sample-lambda-durable-functions)
- [sample-autonomous-cloud-coding-agents](https://github.com/aws-samples/sample-autonomous-cloud-coding-agents)

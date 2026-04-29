# Healthcare Prior Authorization — Multi-Agent Durable Function

A reference architecture for AI-assisted prior authorization using [AWS Lambda Durable Functions](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html) with specialist agents running on [Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore.html). Demonstrates conditional multi-agent orchestration with pause/resume for healthcare PA workflows.

## How it works

A single durable function orchestrates the entire PA lifecycle. A **Document Agent** acts as the planner — it extracts clinical facts and determines which specialist agents are needed. Only the required specialists are invoked in parallel via AgentCore.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  CoordinatorFunction — Lambda Durable Function (up to 14 days)              │
│                                                                             │
│  ┌──────────────────────────┐                                               │
│  │ waitForCallback:         │  Document Agent on AgentCore                  │
│  │   extract-clinical-facts │  Extracts facts + determines routing plan     │
│  │   (PLANNER)              │  → which specialists are needed?              │
│  └──────────┬───────────────┘                                               │
│             │                                                               │
│             ▼ (if missing fields)                                            │
│  ┌──────────────────────────┐                                               │
│  │ waitForCallback:         │  ⏸️  Suspended — provider uploads docs        │
│  │   wait-for-provider-docs │  (up to 72 hours, $0 compute)                 │
│  └──────────┬───────────────┘                                               │
│             │                                                               │
│             ▼                                                               │
│  ┌──────────────────────────────────────────────────────────┐               │
│  │ context.map: 'specialist-agents' (parallel, conditional) │               │
│  │                                                          │               │
│  │  ┌─ waitForCallback: invoke-eligibility ─┐  ← if needed │               │
│  │  │  Eligibility Agent on AgentCore       │              │               │
│  │  └──────────────────────────────────────┘              │               │
│  │  ┌─ waitForCallback: invoke-policy ──────┐  ← if needed │               │
│  │  │  Policy Agent on AgentCore            │              │               │
│  │  └──────────────────────────────────────┘              │               │
│  │  ┌─ waitForCallback: invoke-medical-necessity ┐ ← if needed             │
│  │  │  Medical Necessity Agent on AgentCore      │         │               │
│  │  └───────────────────────────────────────────┘         │               │
│  └──────────┬───────────────────────────────────────────────┘               │
│             │                                                               │
│             ▼ (if low confidence)                                            │
│  ┌──────────────────────────┐                                               │
│  │ waitForCallback:         │  ⏸️  Suspended — human reviewer decides       │
│  │   wait-for-human-review  │  (up to 48 hours)                             │
│  └──────────┬───────────────┘                                               │
│             │                                                               │
│             ▼                                                               │
│  ┌──────────────────────────┐                                               │
│  │ step: submit-prior-auth  │  AT_MOST_ONCE — no duplicate submissions      │
│  └──────────┬───────────────┘                                               │
│             │                                                               │
│             ▼                                                               │
│  ┌──────────────────────────┐                                               │
│  │ waitForCallback:         │  ⏸️  Suspended — payer decides                │
│  │   wait-for-payer-decision│  (up to 14 days)                              │
│  └──────────┬───────────────┘                                               │
│             │                                                               │
│             ▼                                                               │
│  ┌──────────────────────────┐                                               │
│  │ step: notify-provider    │  Send decision to provider                    │
│  └──────────────────────────┘                                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

> **Solid boxes** = always executed | **"← if needed"** = conditionally selected by the Document Agent (planner)

## Key design: Planner-driven conditional routing

The Document Agent serves dual purpose:
1. **Extracts** clinical facts from notes (diagnosis, procedure, evidence)
2. **Plans** which specialists are needed based on the clinical context

Its output includes boolean flags:
```json
{
  "requiresEligibilityCheck": true,
  "requiresPolicyLookup": true,
  "requiresMedicalNecessity": false
}
```

Only flagged specialists are invoked. For example:
- A routine lab reauthorization might only need eligibility check
- A complex surgical PA needs all three specialists
- A medication PA might skip eligibility (already verified) but need policy + necessity

## Durable primitives used

| Primitive | Where | Why |
|-----------|-------|-----|
| `waitForCallback()` | Document Agent, each specialist agent | Async AgentCore invocation — suspend while agent processes |
| `context.map()` | Specialist agents | Fan out selected agents in parallel, each independently checkpointed |
| `waitForCallback()` | Missing docs, human review, payer decision | Suspend for hours/days with $0 compute |
| `AT_MOST_ONCE` | PA submission | Prevent duplicate submissions to payer |
| `retryStrategy` | Payer API calls | Exponential backoff with jitter |

## AgentCore integration pattern

Each agent runs on Bedrock AgentCore as a containerized Strands agent. The durable function invokes them asynchronously:

```typescript
const result = await context.waitForCallback(
  'invoke-eligibility',
  async (callbackId, ctx) => {
    // Send prompt + callbackId to AgentCore
    await invokeAgent(ELIGIBILITY_AGENT_ARN, { ...payload, callbackId });
  },
  { timeout: { minutes: 5 } }
);
// Agent calls SendDurableExecutionCallbackSuccess when done
```

The durable function **suspends** (no compute charges) while each agent processes. When the agent finishes, it sends the callback to resume execution.

## Prerequisites

- AWS account with Bedrock AgentCore access
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) 1.153.1+
- Node.js 24+
- Docker/Finch (for building agent container images)

## Deploy

```bash
# Install dependencies
npm install

# Build and push agent containers (arm64) to ECR
./scripts/build.sh push-agents --region us-west-2

# Build and deploy the full stack (ECR repos, AgentCore runtimes, durable Lambda)
./scripts/build.sh build --region us-west-2
./scripts/build.sh deploy --region us-west-2
```

Or all at once:
```bash
./scripts/build.sh all --region us-west-2
```

## Testing the workflow

After deployment, test the full multi-agent orchestration using the CLI. The workflow suspends at each agent callback — you send callbacks to simulate agent responses and advance the execution.

### Step 1: Start a PA request

```bash
aws lambda invoke \
  --function-name healthcare-pa-coordinator:live \
  --invocation-type Event \
  --region us-west-2 \
  --durable-execution-name "pa-test-$(date +%s)" \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "patientId": "PT-001",
    "providerId": "DR-100",
    "payerId": "PAYER-BCBS",
    "procedureCode": "27447",
    "diagnosisCode": "M17.11",
    "clinicalNotes": "70yo male with severe OA right knee, failed conservative treatment 8 months, BMI 29, no active infections."
  }' \
  /dev/null
```

Note the `DurableExecutionArn` from the response.

### Step 2: Check execution status

```bash
aws lambda get-durable-execution-history \
  --durable-execution-arn "<EXECUTION_ARN>" \
  --region us-west-2 \
  --query 'Events[].{Type:EventType,Name:Name}' \
  --output table
```

### Step 3: Send Document Agent callback (planner result)

Get the callback ID from the execution history (`CallbackStarted` event), then send the planner's routing decision:

```bash
aws lambda send-durable-execution-callback-success \
  --callback-id "<CALLBACK_ID>" \
  --region us-west-2 \
  --cli-binary-format raw-in-base64-out \
  --result '{
    "diagnosis": "M17.11",
    "procedure": "27447",
    "supportingEvidence": ["Failed conservative treatment 8 months", "Severe OA confirmed on imaging"],
    "missingFields": [],
    "requiresEligibilityCheck": true,
    "requiresPolicyLookup": true,
    "requiresMedicalNecessity": true
  }'
```

This resumes the coordinator, which invokes the 3 specialist agents in parallel.

### Step 4: Send specialist agent callbacks

After the specialists are invoked (check history for 3 new `CallbackStarted` events), send each one:

**Eligibility Agent:**
```bash
aws lambda send-durable-execution-callback-success \
  --callback-id "<ELIGIBILITY_CALLBACK_ID>" \
  --region us-west-2 \
  --cli-binary-format raw-in-base64-out \
  --result '{"eligible": true, "planId": "PLAN-BCBS-001", "coverageDetails": "Active PPO"}'
```

**Policy Agent:**
```bash
aws lambda send-durable-execution-callback-success \
  --callback-id "<POLICY_CALLBACK_ID>" \
  --region us-west-2 \
  --cli-binary-format raw-in-base64-out \
  --result '{"covered": true, "requiresPriorAuth": true, "criteria": ["Failed conservative tx 6mo", "BMI<40", "No infection"], "policyId": "POL-BCBS-27447"}'
```

**Medical Necessity Agent:**
```bash
aws lambda send-durable-execution-callback-success \
  --callback-id "<MEDICAL_NECESSITY_CALLBACK_ID>" \
  --region us-west-2 \
  --cli-binary-format raw-in-base64-out \
  --result '{"meetsNecessity": true, "confidence": 0.94, "rationale": "All policy criteria met"}'
```

### Step 5: Send Synthesis Agent callback

After all specialists complete, the coordinator invokes the Synthesis Agent:

```bash
aws lambda send-durable-execution-callback-success \
  --callback-id "<SYNTHESIS_CALLBACK_ID>" \
  --region us-west-2 \
  --cli-binary-format raw-in-base64-out \
  --result '{"recommendation": "approve", "confidence": 0.95, "rationale": "All criteria met, no gaps.", "eligible": true, "gaps": []}'
```

### Step 6: Send Payer Decision callback

The coordinator submits the PA and suspends for the payer's decision (up to 14 days in production):

```bash
aws lambda send-durable-execution-callback-success \
  --callback-id "<PAYER_CALLBACK_ID>" \
  --region us-west-2 \
  --cli-binary-format raw-in-base64-out \
  --result '{"approved": true, "rationale": "Authorized per medical policy POL-BCBS-27447"}'
```

### Step 7: Verify completion

```bash
aws lambda get-durable-execution-history \
  --durable-execution-arn "<EXECUTION_ARN>" \
  --region us-west-2 \
  --query 'Events[-4:].{Type:EventType,Name:Name}' \
  --output table
```

Expected output:
```
|  notify-provider    |  StepStarted          |
|  notify-provider    |  StepSucceeded        |
|  None               |  InvocationCompleted  |
|  pa-e2e2-...       |  ExecutionSucceeded   |
```

### Testing conditional routing

To test with fewer specialists (e.g., only eligibility), send a Document Agent callback with some flags set to `false`:

```bash
--result '{
  "diagnosis": "M17.11",
  "procedure": "27447",
  "supportingEvidence": ["Pre-verified coverage"],
  "missingFields": [],
  "requiresEligibilityCheck": true,
  "requiresPolicyLookup": false,
  "requiresMedicalNecessity": false
}'
```

Only the Eligibility Agent will be invoked — the others are skipped.

### Testing missing documentation flow

To test the provider upload suspension, include missing fields:

```bash
--result '{
  "diagnosis": "M17.11",
  "procedure": "27447",
  "supportingEvidence": [],
  "missingFields": ["imaging_report", "pt_notes"],
  "requiresEligibilityCheck": true,
  "requiresPolicyLookup": true,
  "requiresMedicalNecessity": true
}'
```

The coordinator will suspend at `wait-for-provider-docs` (up to 72 hours). Send the provider upload callback to resume:

```bash
aws lambda send-durable-execution-callback-success \
  --callback-id "<PROVIDER_DOCS_CALLBACK_ID>" \
  --region us-west-2 \
  --cli-binary-format raw-in-base64-out \
  --result '{"additionalNotes": "MRI confirms severe OA, PT discharge summary attached"}'
```

## Project structure

```
src/
  handlers/
    coordinator.ts          # Durable function — orchestrates the PA workflow
    coordinator.test.ts     # Tests with LocalDurableTestRunner
  lib/
    (agent configurations would go here)
agent/
  document-agent/           # Strands agent — clinical extraction + planning
  eligibility-agent/        # Strands agent — payer eligibility check
  policy-agent/             # Strands agent — medical policy lookup
  medical-necessity-agent/  # Strands agent — evidence vs policy comparison
template.yaml               # SAM template with DurableConfig
```

## Production considerations

- **HIPAA compliance**: All data at rest/in transit must be encrypted; execution state contains PHI
- **Agent isolation**: Each AgentCore agent has its own IAM role scoped to its domain
- **Graceful degradation**: If a specialist fails, error is captured and passed to human review
- **Idempotency**: Use `durable-execution-name` with patient+procedure as key
- **Prompt versioning**: Load prompts inside agent containers; agent version is pinned at invocation time

## License

MIT

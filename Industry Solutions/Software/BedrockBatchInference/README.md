# Bedrock Batch Inference

A minimal example showing how to orchestrate Amazon Bedrock batch inference with Lambda Durable Functions. Upload text files to S3, invoke the function, and it processes them all through Bedrock — **suspending at zero compute cost** while the job runs (minutes to hours), then resuming automatically when it completes.

## Architecture

```
 ┌─────────────┐
 │ .txt files  │  You upload >= 100 files to s3://bucket/inputs/
 │ in S3       │
 └──────┬──────┘
        │
┌───────▼──────────────────────────────────────────────────────────┐
│  Durable Function (orchestrator)                                 │
│                                                                  │
│  1. List files in s3://bucket/inputs/                            │
│  2. Read each file, apply prompt template, write JSONL           │
│  3. Submit batch job to Bedrock                                  │
│  4. wait_for_callback() ──── SUSPENDED (no compute) ────┐        │
│  5. Read output, return results                         │        │
└─────────────────────────────────────────────────────────┼────────┘
                                                          │
┌─────────────────────┐     ┌──────────────────────┐      │
│  Amazon Bedrock     │────►│  EventBridge         │      │
│  (batch inference)  │     │  (job state change)  │      │
└─────────────────────┘     └──────────┬───────────┘      │
                                       │                  │
                            ┌──────────▼───────────┐      │
                            │  Relay Lambda        │──────┘
                            │  (sends callback)    │
                            └──────────────────────┘
```

## How it works

1. **List files** — Scans `s3://bucket/inputs/` for `.txt` files.

2. **Build batch input** — Reads each file, wraps its content in a prompt template (`"Summarize the following text:\n\n{content}"`), and writes the batch as a JSONL file in Bedrock's [Converse format](https://docs.aws.amazon.com/bedrock/latest/userguide/batch-inference.html).

3. **Submit job** — Calls `CreateModelInvocationJob` with a `clientRequestToken` derived from the job ID (idempotent on retry).

4. **Suspend** — `context.wait_for_callback()` persists the execution state and releases all compute. The Lambda is not running or billed during this time.

5. **Resume** — When Bedrock finishes, it emits an EventBridge event. The relay Lambda looks up the callback ID from DynamoDB and calls `send_durable_execution_callback_success`, which resumes the orchestrator exactly where it left off.

6. **Return results** — The orchestrator reads the output from S3 and returns a summary.

## Deploy

```bash
sam build
sam deploy --guided
```

## Upload files

Upload at least 100 `.txt` files (Bedrock's non-adjustable minimum per job):

```bash
# Example: generate 100 sample files
BUCKET=$(aws cloudformation describe-stacks \
  --stack-name bedrock-batch-durable \
  --query 'Stacks[0].Outputs[?OutputKey==`BucketName`].OutputValue' \
  --output text)

for i in $(seq -w 1 100); do
  echo "This is document $i about topic $i. It discusses various aspects..." \
    | aws s3 cp - "s3://$BUCKET/inputs/doc-$i.txt"
done
```

## Invoke

```bash
aws lambda invoke \
  --function-name bedrock-batch-durable-orchestrator:live \
  --invocation-type Event \
  --durable-execution-name "batch-run-1" \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  output.json
```

Or with a custom prefix and prompt:

```bash
aws lambda invoke \
  --function-name bedrock-batch-durable-orchestrator:live \
  --invocation-type Event \
  --durable-execution-name "batch-run-2" \
  --payload '{"prefix": "reports/", "max_tokens": 2048}' \
  --cli-binary-format raw-in-base64-out \
  output.json
```

The function returns immediately (async). It will complete once Bedrock finishes processing all files.

## Key durable function concepts demonstrated

| Concept | Where |
|---------|-------|
| `context.step()` — atomic, checkpointed operation | List files, build input, submit job, read output |
| `context.wait_for_callback()` — suspend until external signal | Waiting for Bedrock job completion |
| EventBridge → callback relay pattern | `callback_relay.py` bridges events to durable callbacks |
| Idempotent submission | `clientRequestToken` prevents duplicate Bedrock jobs on replay |
| Graceful error handling | `CallbackError` catch returns failure status instead of crashing |

## Run tests

```bash
pip install -r requirements.txt
pip install aws-durable-execution-sdk-python-testing pytest pytest-mock
pytest test_batch_orchestrator.py -v
```

## Cleanup

```bash
BUCKET=$(aws cloudformation describe-stacks \
  --stack-name bedrock-batch-durable \
  --query 'Stacks[0].Outputs[?OutputKey==`BucketName`].OutputValue' \
  --output text)
aws s3 rm "s3://$BUCKET" --recursive
sam delete
```

## Cost

- **Orchestrator compute**: ~5-10 seconds total (list + read files + submit + read output). Zero cost during the Bedrock wait.
- **Relay Lambda**: One invocation per completed job (~100ms).
- **Bedrock batch**: Billed per input/output token at the [batch pricing rate](https://aws.amazon.com/bedrock/pricing/) (typically 50% off on-demand).
- **S3/DynamoDB**: Negligible for this sample.

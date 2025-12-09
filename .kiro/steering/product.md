# Product Overview

This repository demonstrates AWS Durable Functions - a Lambda extension that enables long-running, stateful workflows that can execute for up to one year.

## Core Capabilities

- **Checkpointing**: Functions save their state at defined points, allowing them to resume after interruptions
- **Durable Promises**: Persistent promises that survive system restarts and failures
- **Long-running executions**: Asynchronous invocations can run up to one year with automatic state recovery
- **Cost-efficient waiting**: Functions can pause without consuming resources during wait periods
- **External callbacks**: Functions can wait for external events (webhooks, human approval, etc.) without staying active

## Example Use Cases

The repository includes a fraud detection example that demonstrates:
- Multi-step transaction processing with risk scoring
- Conditional workflow branching based on fraud scores
- Human-in-the-loop verification via email/SMS callbacks
- Parallel execution of verification channels
- Automatic escalation and authorization flows

## Key Difference from Standard Lambda

Standard Lambda functions have a 15-minute timeout. Durable Functions extend this by checkpointing state and replaying execution, enabling workflows that span hours, days, or months while only paying for active compute time.

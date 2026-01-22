# AWS Durable Functions Examples

This repository contains example projects demonstrating AWS Durable Functions - a Lambda extension that enables long-running, stateful workflows that can execute for up to one year.

## Table of Contents

- [About this Repo](#about-this-repo)
- [What are Durable Functions?](#what-are-durable-functions)
- [Examples](#examples)
- [Getting Started](#getting-started)
- [Learning Resources](#learning-resources)
- [License](#license)

## About this Repo

This repo provides practical examples of AWS Durable Functions organized by industry and use case. Each example demonstrates real-world scenarios where durable execution patterns solve complex business problems that require long-running workflows, human-in-the-loop processes, or external system integrations.

The examples are designed to be:
- **Industry-specific**: Organized by vertical (Financial Services, Healthcare, Retail, etc.) to showcase relevant use cases
- **Production-ready patterns**: Demonstrate best practices for error handling, state management, and workflow orchestration
- **Deployable**: Include complete infrastructure-as-code using AWS CDK or SAM templates

We welcome contributions in the form of fixes to existing examples or new industry-specific use cases. For more information, please see the CONTRIBUTING guide.

This is considered an intermediate learning resource and should be referenced alongside the [AWS Durable Functions documentation](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html).

## What are Durable Functions?

AWS Durable Functions extend AWS Lambda to support long-running, stateful workflows through automatic checkpointing and replay, while only paying for active compute time. Key capabilities include:

- **Long-running executions**: Run workflows for up to one year with automatic state recovery
- **Checkpointing**: Functions save their state at defined points and resume after interruptions
- **Cost-efficient waiting**: Pause execution without consuming resources during wait periods
- **External callbacks**: Wait for external events (webhooks, human approval, third-party APIs) without staying active
- **Durable promises**: Persistent promises that survive system restarts and failures


## Examples

Examples are organized by industry vertical to demonstrate domain-specific use cases:

### Financial Services (FSI)

| Example | Description | Technologies |
|---------|-------------|--------------|
| [Fraud Detection](./Industry/Financial%20Services%20(FSI)/FraudDetection/) | Multi-step transaction processing with risk scoring, human-in-the-loop verification, and conditional workflow branching | Lambda, TypeScript, AWS CDK, SAM |

### Coming Soon

Additional industry verticals and use cases will be added overtime.

## Getting Started

### Prerequisites

Review the README found within the folder of each sample.  Each sample has its own unique set of requirements.

### Quick Start

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd <repository-name>
   ```

2. **Configure the custom CLI model** (enables `aws durable-lambda` commands)
   ```bash
   aws configure add-model --service-model file://./service.json --service-name durable-lambda
   ```

3. **Navigate to an example**
   ```bash
   cd "Industry/Financial Services (FSI)/FraudDetection"
   ```

4. **Follow the example's README** for deployment instructions

### Common Commands

```bash
# Deploy a CDK-based example
cdk bootstrap aws://<ACCOUNT_ID>/<REGION>
cdk deploy

# Deploy a SAM-based example
sam build
sam deploy --guided

# Invoke a durable function
aws durable-lambda invoke --function-name <arn> --region <region> ./output

# List executions
aws durable-lambda list-durable-executions-by-function \
  --function-name <name> --status-filter SUCCEEDED --region <region>
```

## Learning Resources

### Official Resources

- [AWS Durable Functions Documentation](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html) - Official AWS documentation
- [AWS Lambda Developer Guide](https://docs.aws.amazon.com/lambda/latest/dg/) - Comprehensive Lambda documentation
- [AWS CDK Developer Guide](https://docs.aws.amazon.com/cdk/latest/guide/) - Infrastructure as code with CDK
- [AWS Serverless Application Model (SAM)](https://docs.aws.amazon.com/serverless-application-model/) - Simplified serverless deployment

### Example Documentation

Each example includes detailed documentation:
- Architecture diagrams and workflow explanations
- Step-by-step deployment guides
- Testing and invocation instructions
- Best practices and design patterns

## License

This library is licensed under the Apache 2.0 License.

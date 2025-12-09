# Project Structure

## Repository Layout

The repository contains multiple approaches to deploying durable functions:

### CDK-Based Deployment (`cdk-durable-function-typescript/`)

Primary infrastructure-as-code approach using AWS CDK.

```
cdk-durable-function-typescript/
├── bin/                          # CDK app entry point
├── lib/
│   ├── apps/                     # Durable function applications
│   │   ├── my-first-durable-function/   # Example function
│   │   └── FraudDetection/              # Fraud detection workflow
│   ├── custom-resource-handler/  # Lambda for CDK custom resources
│   ├── custom-packages/          # Local SDK development packages
│   └── cdk-durable-function-stack.ts  # Main CDK stack definition
├── docs/                         # Documentation
├── cdk.json                      # CDK configuration
├── deploy.sh                     # Deployment script
└── service.json                  # Custom AWS CLI service model
```

### Standalone Lambda (`FraudDetection-Lambda/`)

Direct Lambda deployment without CDK infrastructure.

```
FraudDetection-Lambda/
├── src/
│   └── index.ts                  # Lambda handler with durable execution
├── test/
│   └── tx.json                   # Test transaction data
├── dist/                         # Compiled JavaScript output
├── package.json
└── tsconfig.json
```

## Key Directories

### `/lib/apps/`

Each subdirectory represents a deployable durable function:
- Must contain `package.json` with dependencies
- Must have `src/index.ts` exporting a `handler` function
- Automatically discovered and deployed by CDK stack
- Each function gets its own IAM role, S3 deployment, and Lambda resource

### `/lib/custom-packages/`

Contains the local development version of the durable execution SDK:
- `aws-durable-execution-sdk-js-development/`: Full SDK source code
- Used via `file:` references in function `package.json` files
- Includes testing utilities, examples, and ESLint plugins

### `/docs/`

Documentation for understanding durable functions:
- `1-intro.md`: Introduction to durable functions
- `2-understanding.md`: How durable functions work
- `3-core-concepts.md`: Key concepts and capabilities
- `4-sdk-functional-spec.md`: SDK API reference
- `5-api-functional-spec.md`: AWS API reference

## Durable Function Structure

Every durable function follows this pattern:

```typescript
import { withDurableExecution, DurableContext, LambdaHandler } from "aws-durable-execution-sdk-js";

export const handler: LambdaHandler<InputType> = withDurableExecution(
  async (event: InputType, context: DurableContext) => {
    // Use context.step(), context.wait(), context.waitForCallback(), etc.
    // All durable operations must use the context object
    return result;
  }
);
```

## Conventions

### Function Naming
- CDK automatically deploys all directories under `lib/apps/`
- Function names match their directory names
- Use kebab-case for function directory names (e.g., `my-first-durable-function`)

### Dependencies
- Durable functions reference the SDK via local file paths
- AWS SDK v3 clients are used for AWS service interactions
- Each function manages its own `node_modules` and dependencies

### Build Artifacts
- TypeScript compiles to `dist/` directory
- Source maps are generated inline
- Declaration files (`.d.ts`) are created alongside JavaScript

### IAM Roles
- Each durable function gets a dedicated IAM role
- Roles are named `<function-name>-DurableFunctionRole`
- Default policy: AdministratorAccess (should be scoped down for production)
- Roles must have permissions for:
  - `lambda:CheckpointDurableExecution`
  - `lambda:GetDurableExecutionState`

### Deployment Flow
1. CDK packages each function in `lib/apps/` as a zip
2. Uploads zip to S3 bucket with timestamped prefix
3. Custom resource handler creates/updates the durable function
4. Function is configured with `DurableConfig` (timeout, retention)

## Testing

- Unit tests use Jest with ts-jest
- Test files: `*.test.ts` or `*.test.js`
- Testing utilities available in `aws-durable-execution-sdk-js-testing`
- Mock durable context for local testing without AWS infrastructure

## Region Configuration

Default region: `eu-south-1`
- Hardcoded in documentation examples
- Change in CDK stack props if deploying to different region

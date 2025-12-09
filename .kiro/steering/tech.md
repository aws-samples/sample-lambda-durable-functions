# Technology Stack

## Core Technologies

- **Runtime**: Node.js (nodejs22.x)
- **Language**: TypeScript 5.x
- **Infrastructure**: AWS CDK v2
- **Build Tool**: TypeScript Compiler (tsc)
- **Testing**: Jest with ts-jest
- **Package Manager**: npm

## Key Dependencies

- `aws-durable-execution-sdk-js`: Core SDK for durable execution patterns
- `aws-cdk-lib`: AWS CDK constructs for infrastructure
- `@aws-sdk/client-*`: AWS SDK v3 clients (S3, Lambda, etc.)
- `esbuild`: Fast JavaScript bundler used by CDK

## AWS Services

- **Lambda**: Durable Functions runtime
- **S3**: Function code storage and deployment
- **IAM**: Role and policy management
- **KMS**: Environment variable encryption
- **CloudWatch**: Logging and monitoring

## Common Commands

### CDK Infrastructure

```bash
# Bootstrap CDK (first time only)
cdk bootstrap aws://<ACCOUNT_ID>/eu-south-1

# Deploy all durable functions
./deploy.sh

# CDK commands
cdk synth          # Synthesize CloudFormation template
cdk deploy         # Deploy stack
cdk destroy        # Tear down stack
```

### Durable Function Development

```bash
# Build a function
cd lib/apps/<function-name>
npm install
npm run build

# Run tests
npm test

# Clean build artifacts
npm run clean
```

### Lambda Function Deployment

```bash
# Build and package
cd FraudDetection-Lambda
npm run build
npm run zip

# Deploy via AWS CLI
aws durable-lambda create-function --function-name <name> \
  --runtime nodejs22.x --role $ROLE_ARN \
  --handler index.handler --code ZipFile=fileb://FraudDetection.zip \
  --memory-size 128
```

### Durable Execution Management

```bash
# Invoke a durable function
aws durable-lambda invoke --function-name <arn> --region eu-south-1 ./output

# List executions
aws durable-lambda list-durable-executions-by-function \
  --function-name <name> --status-filter SUCCEEDED --region eu-south-1

# Get execution history
aws durable-lambda get-durable-execution-history \
  --durable-execution-arn <arn> --region eu-south-1 --include-execution-data

# Send callback
aws durable-lambda send-durable-execution-callback-success \
  --callback-id <id> --result '{"key":"value"}' \
  --cli-binary-format raw-in-base64-out --region eu-south-1
```

### Prerequisites

Install these tools before working with the project:
- npm (Node.js package manager)
- AWS CDK CLI (`npm install -g aws-cdk`)
- jq (JSON processor)
- AWS CLI v2
- Docker Desktop (required for CDK bundling on macOS)
- TypeScript (`npm install -g typescript`)

### Custom CLI Model

The project uses a custom AWS CLI service model for durable-lambda operations:

```bash
aws configure add-model --service-model file://./service.json --service-name durable-lambda
```

This enables the `aws durable-lambda` command namespace.

## Build Configuration

- **Target**: ES2022
- **Module System**: NodeNext (ESM)
- **Source Maps**: Inline
- **Strict Mode**: Enabled with TypeScript strict checks
- **Declaration Files**: Generated for all TypeScript files

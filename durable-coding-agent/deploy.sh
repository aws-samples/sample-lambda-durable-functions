#!/bin/bash
set -euo pipefail

# --- Configuration ---
REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="durable-coding-agent"
ECR_REPO_NAME="coding-agent"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO_NAME}"

echo "=== Deploying Durable Coding Agent ==="
echo "Account: ${ACCOUNT_ID}"
echo "Region:  ${REGION}"
echo ""

# --- Step 1: Create ECR repo (if not exists) ---
echo "[1/5] Creating ECR repository..."
aws ecr describe-repositories --repository-names "${ECR_REPO_NAME}" --region "${REGION}" 2>/dev/null || \
  aws ecr create-repository --repository-name "${ECR_REPO_NAME}" --region "${REGION}"

# --- Step 2: Build and push agent container ---
echo "[2/5] Building and pushing agent container..."
aws ecr get-login-password --region "${REGION}" | finch login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
finch build --platform linux/amd64 -t "${ECR_REPO_NAME}" ./agent
finch tag "${ECR_REPO_NAME}:latest" "${ECR_URI}:latest"
finch push "${ECR_URI}:latest"

# --- Step 3: Build orchestrator ---
echo "[3/5] Building orchestrator..."
cd orchestrator
npm install
npm run build
cd ..

# --- Step 4: Create GitHub token secret (if not exists) ---
echo "[4/5] Ensuring GitHub token secret exists..."
SECRET_NAME="durable-coding-agent/github-token"
if ! aws secretsmanager describe-secret --secret-id "${SECRET_NAME}" --region "${REGION}" 2>/dev/null; then
  echo ""
  echo "  Secret '${SECRET_NAME}' not found. Creating it now."
  echo "  After deployment, store your GitHub token with:"
  echo ""
  echo "    aws secretsmanager put-secret-value \\"
  echo "      --secret-id ${SECRET_NAME} \\"
  echo "      --secret-string 'ghp_YOUR_TOKEN_HERE' \\"
  echo "      --region ${REGION}"
  echo ""
  aws secretsmanager create-secret \
    --name "${SECRET_NAME}" \
    --description "GitHub PAT for the durable coding agent" \
    --secret-string "PLACEHOLDER" \
    --region "${REGION}"
fi

# --- Step 5: Deploy SAM stack ---
echo "[5/5] Deploying SAM stack..."
cd infra
sam build
sam deploy \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    "Region=${REGION}" \
    "ECRRepoName=${ECR_REPO_NAME}" \
  --resolve-s3 \
  --no-confirm-changeset

echo ""
echo "=== Deployment complete ==="
echo ""
echo "Store your GitHub token:"
echo "  aws secretsmanager put-secret-value --secret-id ${SECRET_NAME} --secret-string 'ghp_...' --region ${REGION}"
echo ""
echo "Invoke the agent:"
echo "  aws lambda invoke --function-name durable-coding-agent --cli-binary-format raw-in-base64-out \\"
echo "    --payload '{\"repo\":\"owner/repo\",\"task_description\":\"Add a health check endpoint\"}' response.json"

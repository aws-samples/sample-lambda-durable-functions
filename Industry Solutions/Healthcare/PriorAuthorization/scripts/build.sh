#!/usr/bin/env bash
set -euo pipefail

# Healthcare Prior Authorization — Build & Deploy Script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

usage() {
  cat <<EOF
Usage: $0 <command> [options]

Commands:
  install        Install Lambda dependencies
  build          Build the SAM application
  push-agents    Build + push agent containers to ECR
  deploy         Deploy the full stack (ECR repos, AgentCore runtimes, durable Lambda)
  test           Run unit tests
  all            install + build + push-agents + deploy

Options:
  --region       AWS region (default: us-west-2)
  --stack-name   CloudFormation stack name (default: healthcare-prior-auth)
  --profile      AWS CLI profile

EOF
  exit 1
}

REGION="${AWS_DEFAULT_REGION:-us-west-2}"
STACK_NAME="healthcare-prior-auth"
PROFILE_FLAG=""
COMMAND=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    install|build|push-agents|deploy|test|all) COMMAND="$1"; shift ;;
    --region) REGION="$2"; shift 2 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --profile) PROFILE_FLAG="--profile $2"; shift 2 ;;
    *) usage ;;
  esac
done

[[ -z "$COMMAND" ]] && usage

ACCOUNT_ID=$(aws sts get-caller-identity $PROFILE_FLAG --query Account --output text)
ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

cmd_install() {
  echo "📦 Installing Lambda dependencies..."
  cd "$PROJECT_DIR" && npm install
}

cmd_build() {
  echo "🔨 Building SAM application..."
  cd "$PROJECT_DIR" && sam build
}

cmd_push_agents() {
  echo "🐳 Building and pushing agent containers to ECR..."

  # Login to ECR
  aws ecr get-login-password --region "$REGION" $PROFILE_FLAG | \
    docker login --username AWS --password-stdin "$ECR_REGISTRY"

  AGENTS=(document-agent eligibility-agent policy-agent medical-necessity-agent synthesis-agent)

  for AGENT in "${AGENTS[@]}"; do
    REPO="healthcare-pa/${AGENT}"
    IMAGE="${ECR_REGISTRY}/${REPO}:latest"

    echo "  Building ${AGENT}..."
    docker build \
      -t "$IMAGE" \
      -f "$PROJECT_DIR/agents/Dockerfile" \
      "$PROJECT_DIR/agents/${AGENT}"

    echo "  Pushing ${AGENT}..."
    docker push "$IMAGE"
  done

  echo "✅ All agent images pushed to ECR"
}

cmd_test() {
  echo "🧪 Running tests..."
  cd "$PROJECT_DIR" && npm test
}

cmd_deploy() {
  echo "🚀 Deploying stack to ${REGION}..."
  cd "$PROJECT_DIR"

  sam deploy \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --resolve-s3 \
    --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
    --no-confirm-changeset \
    --no-fail-on-empty-changeset \
    $PROFILE_FLAG
}

case "$COMMAND" in
  install) cmd_install ;;
  build) cmd_build ;;
  push-agents) cmd_push_agents ;;
  deploy) cmd_deploy ;;
  test) cmd_test ;;
  all) cmd_install; cmd_build; cmd_push_agents; cmd_deploy ;;
esac

# GitHub self-hosted runner image for AWS Lambda MicroVMs (no-docker flavor).
#
# Build context is this directory, zipped with this Dockerfile at the root and
# uploaded to S3 (see README). Ubuntu 24.04 base with Node.js (for the Actions
# runner's dependencies), the real GitHub Actions runner, Python 3, and the
# lifecycle-hook server (app.py).
FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg tar gzip git jq unzip python3 && \
    rm -rf /var/lib/apt/lists/*

# AWS CLI v2 (arm64) — jobs commonly invoke `aws`, and it is not in the base.
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscliv2.zip && \
    unzip -q /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/awscliv2.zip /tmp/aws

# GitHub Actions runner (arm64). Pin the version explicitly.
ENV RUNNER_VERSION=2.337.0
WORKDIR /actions-runner
RUN curl -fsSL -O "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz" && \
    tar xzf "actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz" && \
    rm "actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz" && \
    ./bin/installdependencies.sh && \
    rm -rf /var/lib/apt/lists/*

# Lifecycle-hook server + runner launcher.
COPY entrypoint.sh /actions-runner/entrypoint.sh
COPY app.py /actions-runner/app.py
RUN chmod +x /actions-runner/entrypoint.sh

# Required to run the runner as root (default in containers).
ENV RUNNER_ALLOW_RUNASROOT=1

# Port the lifecycle-hook server listens on; must match the image hooks `port`
# and the RunnerPort stack parameter.
ENV RUNNER_PORT=8080
EXPOSE 8080

# The MicroVM runs this as its application process; the platform calls the
# lifecycle hooks against it, and the durable orchestrator calls /configure.
CMD ["python3", "/actions-runner/app.py"]

#!/usr/bin/env python3
"""Test the code generation pipeline locally with S3 output."""
import sys
sys.path.insert(0, "agent/src")

from pipeline import run_coding_task

result = run_coding_task(
    repo_url="https://github.com/example/my-app.git",
    task_description="Create a simple Express.js health check endpoint at GET /health that returns {status: 'ok'}",
    model_id="us.anthropic.claude-sonnet-4-6",
    task_id="test-001",
    output_bucket="durable-coding-agent-output-637423582422",
)

print("\n=== Result ===")
for k, v in result.items():
    print(f"  {k}: {v}")

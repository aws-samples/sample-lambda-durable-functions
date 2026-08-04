# Copyright (c) 2025 Amazon Web Services, Inc. or its affiliates.
# All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Bedrock Batch Inference orchestrator using Lambda Durable Functions.

Processes text files from an S3 prefix: lists files, reads their content, packs
them into a JSONL batch, submits to Bedrock, then suspends (zero compute cost)
until the job completes via EventBridge callback.

Demonstrates:
  - context.step() for atomic operations (list files, build input, submit, read output)
  - context.wait_for_callback() for async external-system integration
  - EventBridge → callback relay pattern for event-driven resumption
"""
import json
import os
import uuid

import boto3
from aws_lambda_powertools import Logger

from aws_durable_execution_sdk_python import DurableContext, durable_execution
from aws_durable_execution_sdk_python.config import Duration
from aws_durable_execution_sdk_python.errors import CallbackError
from aws_durable_execution_sdk_python.waits import WaitForCallbackConfig

logger = Logger(service="bedrock-batch")

bedrock = boto3.client("bedrock")
s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")

BUCKET = os.environ["BUCKET"]
INPUT_PREFIX = os.environ.get("INPUT_PREFIX", "inputs/")
MODEL_ID = os.environ["MODEL_ID"]
ROLE_ARN = os.environ["BEDROCK_ROLE_ARN"]
CALLBACK_TABLE = os.environ["CALLBACK_TABLE"]
PROMPT_TEMPLATE = os.environ.get(
    "PROMPT_TEMPLATE", "Summarize the following text:\n\n{content}"
)
# Bedrock enforces a non-adjustable minimum of 100 records per batch job.
MIN_RECORDS = 100


@durable_execution
def handler(event: dict, context: DurableContext) -> dict:
    """Orchestrate a Bedrock batch inference job over text files in S3.

    event:
        prefix: str — S3 prefix containing .txt files (default: "inputs/")
        max_tokens: int — max output tokens per record (default: 1024)
    """
    prefix = event.get("prefix", INPUT_PREFIX)
    max_tokens = event.get("max_tokens", 1024)

    # Step 1: List text files in the input prefix
    files = context.step(
        lambda _: list_input_files(prefix),
        name="list-files",
    )
    context.logger.info(f"Found {len(files)} files in s3://{BUCKET}/{prefix}")

    if len(files) < MIN_RECORDS:
        raise ValueError(
            f"Bedrock batch requires >= {MIN_RECORDS} files, found {len(files)}. "
            f"Upload more .txt files to s3://{BUCKET}/{prefix}"
        )

    # Step 2: Generate a unique job ID
    job_id = context.step(lambda _: str(uuid.uuid4()), name="generate-job-id")

    # Step 3: Read files and write JSONL input to S3
    record_count = context.step(
        lambda _: build_batch_input(job_id, files, max_tokens),
        name="build-input",
    )
    context.logger.info(f"Built batch input: {record_count} records")

    # Step 4: Submit the Bedrock batch job
    job_arn = context.step(lambda _: submit_job(job_id), name="submit-job")
    context.logger.info(f"Submitted Bedrock job: {job_arn}")

    # Step 5: Wait for Bedrock to finish (suspends — zero compute cost)
    def register_callback(callback_id: str):
        table = dynamodb.Table(CALLBACK_TABLE)
        table.put_item(Item={"jobArn": job_arn, "callbackId": callback_id})

    try:
        bedrock_result = context.wait_for_callback(
            submitter=register_callback,
            config=WaitForCallbackConfig(timeout=Duration.from_hours(4)),
            name="wait-for-bedrock",
        )
    except CallbackError as e:
        context.logger.error(f"Bedrock job failed or timed out: {e}")
        return {"jobId": job_id, "status": "failed", "error": str(e)}

    context.logger.info(f"Bedrock job completed: {bedrock_result.get('status')}")

    # Step 6: Read output from S3
    output = context.step(lambda _: read_output(job_arn), name="read-output")

    return {
        "jobId": job_id,
        "bedrockJobArn": job_arn,
        "status": bedrock_result.get("status", "Completed"),
        "filesProcessed": record_count,
        "sampleOutput": output[:3],
    }


def list_input_files(prefix: str) -> list:
    """List all .txt files under the given S3 prefix."""
    files = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".txt"):
                files.append(obj["Key"])
    return sorted(files)


def build_batch_input(job_id: str, file_keys: list, max_tokens: int) -> int:
    """Read each text file from S3 and pack into a Converse-format JSONL."""
    lines = []
    for i, key in enumerate(file_keys):
        content = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
        prompt = PROMPT_TEMPLATE.format(content=content)

        record = {
            "recordId": f"file-{i:05d}",
            "modelInput": {
                "messages": [
                    {"role": "user", "content": [{"text": prompt}]}
                ],
                "inferenceConfig": {"maxTokens": max_tokens},
            },
        }
        lines.append(json.dumps(record, separators=(",", ":")))

    key = f"jobs/{job_id}/input.jsonl"
    s3.put_object(Bucket=BUCKET, Key=key, Body="\n".join(lines).encode("utf-8"))
    return len(lines)


def submit_job(job_id: str) -> str:
    """Submit the Bedrock batch inference job. Idempotent via clientRequestToken."""
    response = bedrock.create_model_invocation_job(
        jobName=f"durable-batch-{job_id[:8]}",
        clientRequestToken=job_id.replace("-", ""),
        modelId=MODEL_ID,
        modelInvocationType="Converse",
        roleArn=ROLE_ARN,
        inputDataConfig={
            "s3InputDataConfig": {
                "s3Uri": f"s3://{BUCKET}/jobs/{job_id}/input.jsonl",
                "s3InputFormat": "JSONL",
            }
        },
        outputDataConfig={
            "s3OutputDataConfig": {"s3Uri": f"s3://{BUCKET}/jobs/{job_id}/output/"}
        },
        timeoutDurationInHours=2,
    )
    return response["jobArn"]


def read_output(job_arn: str) -> list:
    """Read results from the Bedrock batch output location."""
    job = bedrock.get_model_invocation_job(jobIdentifier=job_arn)
    output_uri = job["outputDataConfig"]["s3OutputDataConfig"]["s3Uri"]

    path = output_uri.replace("s3://", "")
    bucket, prefix = path.split("/", 1)

    results = []
    response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    for obj in response.get("Contents", []):
        if not obj["Key"].endswith(".jsonl.out"):
            continue
        body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode("utf-8")
        for line in body.strip().split("\n"):
            if line:
                results.append(json.loads(line))

    return results

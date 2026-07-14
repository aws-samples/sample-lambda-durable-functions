# Copyright (c) 2025 Amazon Web Services, Inc. or its affiliates.
# All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
EventBridge → Durable Function callback relay.

Triggered by EventBridge when a Bedrock batch inference job reaches a terminal
state. Looks up the callback ID stored by the orchestrator and sends it to
resume the durable execution.
"""
import json
import os

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

logger = Logger(service="bedrock-batch-relay")

lambda_client = boto3.client("lambda")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["CALLBACK_TABLE"])


def handler(event, context):
    """Relay Bedrock job state-change events as durable callbacks.

    EventBridge detail shape:
        {"batchJobArn": "...", "status": "Completed|Failed|Expired|..."}
    """
    detail = event.get("detail", {})
    job_arn = detail.get("batchJobArn")
    status = detail.get("status", "")

    if not job_arn:
        logger.warning("No batchJobArn in event", extra={"event": event})
        return {"sent": False, "reason": "no_job_arn"}

    # Look up callback ID
    item = table.get_item(Key={"jobArn": job_arn}).get("Item")
    if not item:
        logger.info("No callback mapping — not our job", extra={"jobArn": job_arn})
        return {"sent": False, "reason": "not_tracked"}

    callback_id = item["callbackId"]
    payload = json.dumps({
        "batchJobArn": job_arn,
        "status": status,
        "failureMessage": detail.get("failureMessage"),
    })

    try:
        lambda_client.send_durable_execution_callback_success(
            CallbackId=callback_id,
            Payload=payload.encode("utf-8"),
        )
        logger.info("Callback sent", extra={"jobArn": job_arn, "status": status})
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ResourceNotFoundException", "InvalidParameterValueException"):
            logger.warning("Callback expired or invalid", extra={"callbackId": callback_id})
        else:
            raise

    # Cleanup (best-effort — TTL is the backstop)
    try:
        table.delete_item(Key={"jobArn": job_arn})
    except ClientError:
        pass

    return {"sent": True, "jobArn": job_arn, "status": status}

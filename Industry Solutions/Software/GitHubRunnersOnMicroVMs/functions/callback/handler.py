"""Callback handler: the runner POSTs here when its GitHub job finishes.

It resumes the suspended durable execution by calling
SendDurableExecutionCallbackSuccess (or ...Failure on job failure).

Expected request body (JSON):
  {
    "callbackId": "<the id the orchestrator handed the runner>",
    "status": "success" | "failure",
    "result":  { ... }    # optional, on success
    "error":   { "type": "...", "message": "..." }   # optional, on failure
  }
"""
from __future__ import annotations

import json

import boto3

lambda_client = boto3.client("lambda")


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event, _context):
    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "invalid JSON body"})

    callback_id = payload.get("callbackId")
    if not callback_id:
        return _response(400, {"error": "callbackId is required"})

    status = (payload.get("status") or "success").lower()

    try:
        if status == "failure":
            err = payload.get("error") or {}
            lambda_client.send_durable_execution_callback_failure(
                CallbackId=callback_id,
                ErrorType=str(err.get("type", "RunnerJobFailed"))[:256],
                ErrorMessage=str(err.get("message", "runner reported failure"))[:1024],
            )
        else:
            lambda_client.send_durable_execution_callback_success(
                CallbackId=callback_id,
                Result=json.dumps(payload.get("result", {"status": "success"})),
            )
    except lambda_client.exceptions.ResourceNotFoundException:
        # Callback already consumed or expired (e.g. orchestrator timed out).
        return _response(409, {"error": "callback not found or already completed"})

    return _response(200, {"ok": True, "callbackId": callback_id})

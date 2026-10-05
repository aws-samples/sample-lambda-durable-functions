"""Durable orchestrator for ephemeral GitHub self-hosted runners on MicroVMs.

Each AWS API call is a durable step, checkpointed and never re-executed on
replay. The flow:

  1. step  read-config        pull image ARN/version, exec role, timeout, port (SSM)
  2.       create_callback    mint the callbackId up front
  3. step  run-microvm        boot ONE runner VM; runHookPayload carries only the
                              small {callbackId, callbackUrl} (always < 4096)
  4. step  create-auth-token  short-lived token scoped to the runner port
  5. step  configure-runner   POST the JIT config to the VM's /configure ingress,
                              with retry/backoff (the proxy 502s for a few seconds
                              during snapshot restore — expected, we retry)
  6.       callback.result()  SUSPEND with zero compute cost until the runner POSTs
                              /callback -> SendDurableExecutionCallback* resumes us
  7. step  terminate-microvm  ephemeral teardown, always attempted

Why the JIT config goes through /configure and not runHookPayload: a real GitHub
encoded_jit_config is ~4KB and runHookPayload is capped at 4096 chars — it does
not fit even gzipped reliably. The ingress channel has no such cap, so the large
config goes there and only the tiny callback coordinates ride in runHookPayload.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import boto3

from microvm_api import MicrovmApiError, MicrovmClient

from aws_durable_execution_sdk_python import DurableContext, durable_execution
from aws_durable_execution_sdk_python.context import StepContext, durable_step
from aws_durable_execution_sdk_python.exceptions import CallbackError

microvm = MicrovmClient()
ssm = boto3.client("ssm")

EXECUTION_ROLE_ARN = os.environ["EXECUTION_ROLE_ARN"]
IMAGE_ARN_PARAM = os.environ["IMAGE_ARN_PARAM"]
IMAGE_VERSION_PARAM = os.environ["IMAGE_VERSION_PARAM"]
CALLBACK_TIMEOUT_PARAM = os.environ["CALLBACK_TIMEOUT_PARAM"]
RUNNER_PORT_PARAM = os.environ["RUNNER_PORT_PARAM"]


@durable_step
def read_config(_: StepContext) -> dict:
    """Load runtime configuration from Parameter Store."""
    names = [IMAGE_ARN_PARAM, IMAGE_VERSION_PARAM, CALLBACK_TIMEOUT_PARAM, RUNNER_PORT_PARAM]
    resp = ssm.get_parameters(Names=names)
    values = {p["Name"]: p["Value"] for p in resp["Parameters"]}
    return {
        "image_arn": values[IMAGE_ARN_PARAM],
        "image_version": values[IMAGE_VERSION_PARAM],
        "callback_timeout_seconds": int(values[CALLBACK_TIMEOUT_PARAM]),
        "runner_port": int(values[RUNNER_PORT_PARAM]),
    }


@durable_step
def run_microvm(_: StepContext, cfg: dict, callback_id: str, callback_url: str) -> dict:
    """Boot one ephemeral runner VM. Returns endpoint + microvmId.

    runHookPayload carries only the small callback coordinates (always well
    under the 4096-char limit). The large JIT config is delivered separately via
    the /configure ingress (see configure_runner).
    """
    payload = json.dumps({"callbackId": callback_id, "callbackUrl": callback_url})
    resp = microvm.run_microvm(
        imageIdentifier=cfg["image_arn"],
        imageVersion=cfg["image_version"],
        executionRoleArn=EXECUTION_ROLE_ARN,
        idlePolicy={
            "maxIdleDurationSeconds": 900,
            "suspendedDurationSeconds": 300,
            "autoResumeEnabled": False,
        },
        runHookPayload=payload,
    )
    return {"microvm_id": resp["microvmId"], "endpoint": resp["endpoint"]}


@durable_step
def create_auth_token(_: StepContext, microvm_id: str, port: int) -> str:
    """Mint a short-lived (60 min) auth token scoped to the runner port."""
    resp = microvm.create_microvm_auth_token(
        microvm_id,
        expirationInMinutes=60,
        allowedPorts=[{"port": port}],
    )
    return resp["authToken"]["X-aws-proxy-auth"]


@durable_step
def configure_runner(
    _: StepContext, endpoint: str, token: str, port: int, jit_config: str
) -> dict:
    """POST the JIT config to the running VM's /configure ingress, with retry.

    The proxy can return 502 for the first few seconds after RunMicrovm while the
    snapshot is restored (documented behavior) — so we retry with backoff rather
    than fail. On success the runner starts its one job and will call back.
    """
    base = endpoint.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"https://{base}"
    body = json.dumps({"jitConfig": jit_config}).encode()

    last_err = None
    for attempt in range(8):  # ~ up to ~40s of retries
        try:
            req = urllib.request.Request(
                url=f"{base}/configure",
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-aws-proxy-auth": token,
                    "X-aws-proxy-port": str(port),
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - AWS endpoint
                resp.read()
            return {"configured": True, "attempts": attempt + 1}
        except urllib.error.HTTPError as exc:
            # 502/503 during restore -> retry; other codes are real failures.
            if exc.code in (502, 503):
                last_err = exc
                time.sleep(min(2 ** attempt, 8))
                continue
            raise
        except urllib.error.URLError as exc:
            last_err = exc
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"/configure did not succeed after retries: {type(last_err).__name__}")


@durable_step
def terminate_microvm(_: StepContext, microvm_id: str) -> dict:
    """Ephemeral teardown. Idempotent: swallow 'already gone'."""
    try:
        microvm.terminate_microvm(microvm_id)
        return {"terminated": microvm_id}
    except MicrovmApiError as exc:
        if exc.status == 404:
            return {"terminated": microvm_id, "note": "already gone"}
        raise


@durable_execution
def handler(event: dict, context: DurableContext) -> dict:
    job = event.get("github", {})
    callback_url = event["callbackUrl"]
    jit_config = job.get("jitConfig", "")

    cfg = context.step(read_config(), name="read-config")

    # Mint the callbackId up front so it rides in runHookPayload (/run), and the
    # runner has it before we push the JIT config.
    callback = context.create_callback(name="runner-job")

    vm = context.step(
        run_microvm(cfg, callback.callback_id, callback_url),
        name="run-microvm",
    )
    token = context.step(
        create_auth_token(vm["microvm_id"], cfg["runner_port"]),
        name="create-auth-token",
    )
    context.step(
        configure_runner(vm["endpoint"], token, cfg["runner_port"], jit_config),
        name="configure-runner",
    )

    try:
        # Suspends with zero compute cost until the runner POSTs to /callback.
        result = callback.result()
        status = "completed"
    except CallbackError as err:
        context.logger.warning("Runner callback failed/timed out: %s", err)
        result = {"error_type": getattr(err, "error_type", "Unknown")}
        status = "failed"

    context.step(terminate_microvm(vm["microvm_id"]), name="terminate-microvm")
    return {"status": status, "microvmId": vm["microvm_id"], "result": result}

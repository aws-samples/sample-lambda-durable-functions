"""CloudFormation custom resource that manages a Lambda MicroVM *image*.

Lambda MicroVMs have no native CloudFormation resource type, so the image is
created/deleted through the `lambda` (lambda-microvms) API from here:

  Create -> CreateMicrovmImage, return imageArn + imageVersion as resource data
  Delete -> DeleteMicrovmImage
  Update -> treated as a replacement (CFN will Create the new, then Delete old)

Response data is surfaced to the template via Fn::GetAtt (ImageArn, ImageVersion).
"""
from __future__ import annotations

import logging
import time

import urllib3

from microvm_api import MicrovmApiError, MicrovmClient

log = logging.getLogger()
log.setLevel(logging.INFO)

microvm = MicrovmClient()
_http = urllib3.PoolManager()

# Image-build hooks are strongly recommended: /ready lets the platform snapshot
# only after the app has fully booted, and /validate lets it prefetch the hot
# parts of the snapshot. Both reduce cold-start latency for every RunMicrovm.
_HOOKS = {
    "port": None,  # filled in from the RunnerPort property
    "microvmImageHooks": {
        "ready": "ENABLED",
        "readyTimeoutInSeconds": 120,
        "validate": "ENABLED",
        "validateTimeoutInSeconds": 120,
    },
    "microvmHooks": {
        "run": "ENABLED",
        "runTimeoutInSeconds": 10,
        "terminate": "ENABLED",
        "terminateTimeoutInSeconds": 10,
    },
}


def _resolve_base_image(explicit: str) -> str:
    """Use the caller-provided base image, else the newest managed one."""
    if explicit:
        return explicit
    images = microvm.list_managed_microvm_images().get("items", [])
    if not images:
        raise RuntimeError("No managed MicroVM base images available in this region")
    # Prefer an al2023 image; fall back to the first listed.
    chosen = next((i for i in images if "al2023" in i.get("imageArn", "")), images[0])
    return chosen["imageArn"]


def _create(props: dict) -> dict:
    base_image_arn = _resolve_base_image(props.get("BaseImageArn", ""))
    hooks = dict(_HOOKS)
    hooks["port"] = int(props["RunnerPort"])

    resp = microvm.create_microvm_image(
        name=props["Name"],
        baseImageArn=base_image_arn,
        buildRoleArn=props["BuildRoleArn"],
        codeArtifact={"uri": props["CodeArtifactUri"]},
        hooks=hooks,
        description="GitHub self-hosted runner image (managed by CloudFormation)",
    )
    image_arn = resp["imageArn"]

    # Wait for the build to reach an ACTIVE version so downstream RunMicrovm works.
    version = _wait_for_active(image_arn)
    return {
        "PhysicalResourceId": image_arn,
        "Data": {"ImageArn": image_arn, "ImageVersion": version},
    }


def _wait_for_active(image_arn: str, attempts: int = 60, delay: int = 15) -> str:
    """Poll GetMicrovmImage until a version is ACTIVE (build can take minutes)."""
    for _ in range(attempts):
        img = microvm.get_microvm_image(image_arn)
        active = img.get("latestActiveImageVersion")
        if active:
            return active
        if img.get("latestFailedImageVersion"):
            raise RuntimeError(f"MicroVM image build failed for {image_arn}")
        time.sleep(delay)
    raise TimeoutError(f"MicroVM image {image_arn} did not become ACTIVE in time")


def _delete(physical_id: str) -> None:
    if not physical_id or not physical_id.startswith("arn:"):
        return  # nothing was created (e.g. a failed create)
    try:
        microvm.delete_microvm_image(physical_id)
    except MicrovmApiError as exc:
        if exc.status == 404:
            return  # already gone
        raise


def _respond(event, context, status, physical_id, data=None, reason=None):
    body = {
        "Status": status,
        "Reason": reason or f"See CloudWatch log stream {context.log_stream_name}",
        "PhysicalResourceId": physical_id or context.log_stream_name,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data or {},
    }
    import json

    encoded = json.dumps(body).encode("utf-8")
    _http.request(
        "PUT",
        event["ResponseURL"],
        body=encoded,
        headers={"content-type": "", "content-length": str(len(encoded))},
    )


def handler(event, context):
    log.info("Request: %s", {k: event.get(k) for k in ("RequestType", "LogicalResourceId")})
    request_type = event["RequestType"]
    props = event.get("ResourceProperties", {})
    physical_id = event.get("PhysicalResourceId")

    try:
        if request_type == "Create":
            result = _create(props)
            _respond(event, context, "SUCCESS", result["PhysicalResourceId"], result["Data"])
        elif request_type == "Update":
            # Image artifact/base changed -> build a new image (new physical id).
            # CloudFormation deletes the old physical id afterwards.
            result = _create(props)
            _respond(event, context, "SUCCESS", result["PhysicalResourceId"], result["Data"])
        elif request_type == "Delete":
            _delete(physical_id)
            _respond(event, context, "SUCCESS", physical_id)
        else:
            _respond(event, context, "FAILED", physical_id, reason=f"Unknown type {request_type}")
    except Exception as exc:  # noqa: BLE001 - surface any failure back to CFN
        log.exception("Custom resource failed")
        _respond(event, context, "FAILED", physical_id, reason=str(exc))

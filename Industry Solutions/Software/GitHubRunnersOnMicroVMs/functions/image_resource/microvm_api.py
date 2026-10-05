"""Minimal client for the AWS Lambda MicroVMs API (service date 2025-09-09).

The MicroVMs API is a REST-JSON API served on the standard Lambda endpoint
(https://lambda.<region>.amazonaws.com), under the /2025-09-09/ path prefix,
signed with SigV4 using the "lambda" signing name.

As of now this API ships in the AWS CLI's bundled model but NOT in the public
boto3/botocore that the Lambda Python runtime uses — there is no
boto3.client("lambda-microvms"), and the "lambda" client has no
create_microvm_image/run_microvm methods. So we call the REST endpoints directly
with SigV4-signed requests built from stock botocore primitives, which ARE
present in the runtime. Swap this for boto3 calls once botocore ships the model.

Wire contract (harvested from `aws lambda-microvms ... --debug`):
  GET    /2025-09-09/managed-microvm-images
  POST   /2025-09-09/microvm-images                      (CreateMicrovmImage)
  GET    /2025-09-09/microvm-images/{urlencoded-arn}     (GetMicrovmImage)
  DELETE /2025-09-09/microvm-images/{urlencoded-arn}     (DeleteMicrovmImage)
  POST   /2025-09-09/microvms                            (RunMicrovm)
  POST   /2025-09-09/microvms/{id}/auth-token            (CreateMicrovmAuthToken)
  DELETE /2025-09-09/microvms/{id}                        (TerminateMicrovm)
"""
from __future__ import annotations

import json
import os
import urllib.parse
import uuid

import botocore.session
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.httpsession import URLLib3Session

_API_VERSION = "2025-09-09"
_SIGNING_NAME = "lambda"


class MicrovmApiError(Exception):
    """Raised on a non-2xx response. Carries status + raw body for diagnostics."""

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"MicroVM API error {status}: {body[:500]}")


class MicrovmClient:
    def __init__(self, region: str | None = None):
        self._session = botocore.session.get_session()
        self._region = (
            region
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1"
        )
        self._endpoint = f"https://lambda.{self._region}.amazonaws.com"
        self._http = URLLib3Session()

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self._endpoint}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        request = AWSRequest(method=method, url=url, data=data, headers=headers)

        creds = self._session.get_credentials()
        if creds is None:
            raise MicrovmApiError(0, "no AWS credentials available")
        SigV4Auth(creds.get_frozen_credentials(), _SIGNING_NAME, self._region).add_auth(request)

        resp = self._http.send(request.prepare())
        text = resp.text or ""
        if resp.status_code >= 300:
            raise MicrovmApiError(resp.status_code, text)
        return json.loads(text) if text else {}

    @staticmethod
    def _enc(identifier: str) -> str:
        # Path-segment encode an ARN or id (ARNs contain ':' which must be escaped).
        return urllib.parse.quote(identifier, safe="")

    # ── Image operations ────────────────────────────────────────────────────
    def list_managed_microvm_images(self) -> dict:
        return self._call("GET", f"/{_API_VERSION}/managed-microvm-images")

    def create_microvm_image(self, **kwargs) -> dict:
        kwargs.setdefault("clientToken", str(uuid.uuid4()))
        return self._call("POST", f"/{_API_VERSION}/microvm-images", kwargs)

    def get_microvm_image(self, image_identifier: str) -> dict:
        return self._call("GET", f"/{_API_VERSION}/microvm-images/{self._enc(image_identifier)}")

    def delete_microvm_image(self, image_identifier: str) -> dict:
        return self._call("DELETE", f"/{_API_VERSION}/microvm-images/{self._enc(image_identifier)}")

    # ── MicroVM operations ──────────────────────────────────────────────────
    def run_microvm(self, **kwargs) -> dict:
        kwargs.setdefault("clientToken", str(uuid.uuid4()))
        return self._call("POST", f"/{_API_VERSION}/microvms", kwargs)

    def create_microvm_auth_token(self, microvm_identifier: str, **kwargs) -> dict:
        return self._call(
            "POST", f"/{_API_VERSION}/microvms/{self._enc(microvm_identifier)}/auth-token", kwargs
        )

    def terminate_microvm(self, microvm_identifier: str) -> dict:
        return self._call("DELETE", f"/{_API_VERSION}/microvms/{self._enc(microvm_identifier)}")

"""Dispatch handler: receives the GitHub webhook, verifies its signature, mints a
just-in-time (JIT) runner config via the GitHub App, and starts a durable
execution.

Edge responsibilities (all run HERE, not in the orchestrator):
  1. Verify GitHub's `X-Hub-Signature-256` HMAC over the RAW request body.
  2. Filter for `workflow_job` action=queued carrying the required runner label.
  3. Mint `encoded_jit_config` from GitHub (App JWT -> installation token ->
     generate-jitconfig), following GitHub's just-in-time runner sample.
  4. Async-invoke the durable orchestrator with the JIT config on the job.

Credential model (same approach as the webhook secret): the GitHub App
credentials live in an SSM SecureString parameter you create out-of-band — see
the PLACEHOLDER note on APP_CREDENTIALS_PARAM below. Nothing secret is baked
into the template or the image.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

import boto3
import jwt  # PyJWT[crypto]; RS256 signing needs the `cryptography` extra

lambda_client = boto3.client("lambda")
ssm = boto3.client("ssm")

ORCHESTRATOR_ALIAS_ARN = os.environ["ORCHESTRATOR_ALIAS_ARN"]
WEBHOOK_SECRET_PARAM = os.environ["WEBHOOK_SECRET_PARAM"]

# ── GitHub App config (minting) ──────────────────────────────────────────────
# PLACEHOLDER — FILL IN:
#   APP_CREDENTIALS_PARAM points at an SSM SecureString you create yourself,
#   holding the GitHub App credentials as a JSON object:
#       {
#         "appId": "123456",              # the GitHub App ID (numeric string)
#         "installationId": "12345678",   # the installation on your org/repo
#         "privateKey": "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----\n"
#       }
#   The PEM newlines must be JSON-escaped as \n. Create it with, e.g.:
#       PRIVATE_KEY=$(jq -Rs . < app-private-key.pem)
#       aws ssm put-parameter --type SecureString \
#         --name /gh-microvm-runners/github/app-credentials \
#         --value "{\"appId\":\"123456\",\"installationId\":\"12345678\",\"privateKey\":${PRIVATE_KEY}}"
#   Until this parameter exists and is populated, minting is skipped and jobs are
#   dispatched with an empty jitConfig (the runner will report NoJitConfig).
APP_CREDENTIALS_PARAM = os.environ.get("APP_CREDENTIALS_PARAM", "")

# Label a job must carry to trigger a JIT runner (sample default: lambda-microvms).
REQUIRED_RUNNER_LABEL = os.environ.get("REQUIRED_RUNNER_LABEL", "lambda-microvms")
# Runner group the JIT runner registers into (GitHub-assigned numeric id).
RUNNER_GROUP_ID = int(os.environ.get("RUNNER_GROUP_ID", "1"))

GITHUB_API = os.environ.get("GITHUB_API_BASE", "https://api.github.com")

_SIGNATURE_HEADER = "x-hub-signature-256"

# Caches across warm invocations (reset on cold start).
_secret_cache: bytes | None = None
_app_creds_cache: dict | None = None


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


# ── Webhook signature verification ───────────────────────────────────────────
def _get_secret() -> bytes:
    global _secret_cache
    if _secret_cache is None:
        resp = ssm.get_parameter(Name=WEBHOOK_SECRET_PARAM, WithDecryption=True)
        _secret_cache = resp["Parameter"]["Value"].encode("utf-8")
    return _secret_cache


def _raw_body_bytes(event) -> bytes:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body.encode("utf-8")


def _header(event, name: str) -> str | None:
    headers = event.get("headers") or {}
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _signature_valid(event) -> bool:
    sent = _header(event, _SIGNATURE_HEADER)
    if not sent or not sent.startswith("sha256="):
        return False
    try:
        secret = _get_secret()
    except Exception:  # noqa: BLE001 - fail closed if the secret can't be read
        return False
    expected = "sha256=" + hmac.new(secret, _raw_body_bytes(event), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sent, expected)


# ── GitHub App auth + JIT minting ────────────────────────────────────────────
def _get_app_creds() -> dict | None:
    """Load the GitHub App credentials JSON from SSM. None if not configured."""
    global _app_creds_cache
    if not APP_CREDENTIALS_PARAM:
        return None
    if _app_creds_cache is None:
        resp = ssm.get_parameter(Name=APP_CREDENTIALS_PARAM, WithDecryption=True)
        _app_creds_cache = json.loads(resp["Parameter"]["Value"])
    return _app_creds_cache


def _app_jwt(creds: dict) -> str:
    """RS256-signed GitHub App JWT (max 10-min lifetime; GitHub allows 60s skew)."""
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 540, "iss": creds["appId"]}
    return jwt.encode(payload, creds["privateKey"], algorithm="RS256")


def _github_post(path: str, token: str, token_scheme: str, body: dict | None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url=f"{GITHUB_API}{path}",
        data=data,
        method="POST",
        headers={
            "Authorization": f"{token_scheme} {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "gh-microvm-runners",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - api.github.com
        return json.loads(resp.read() or b"{}")


def _installation_token(creds: dict) -> str:
    """Exchange the App JWT for a short-lived installation access token."""
    app_jwt = _app_jwt(creds)
    out = _github_post(
        f"/app/installations/{creds['installationId']}/access_tokens",
        token=app_jwt,
        token_scheme="Bearer",
        body=None,
    )
    return out["token"]


def _mint_jit_config(creds: dict, owner_repo: str, runner_name: str, labels: list[str]) -> str:
    """Call generate-jitconfig and return the plain encoded_jit_config.

    Uses the repo endpoint when owner_repo is "owner/repo"; otherwise treats the
    value as an org login and uses the org endpoint. Mirrors GitHub's JIT sample.

    Returns the raw config; the orchestrator wraps it in the runHookPayload
    envelope and gzips the whole thing to fit the 4096-char limit.
    """
    inst_token = _installation_token(creds)
    body = {
        "name": runner_name,
        "runner_group_id": RUNNER_GROUP_ID,
        "labels": labels or [REQUIRED_RUNNER_LABEL],
    }
    if "/" in owner_repo:
        path = f"/repos/{owner_repo}/actions/runners/generate-jitconfig"
    else:
        path = f"/orgs/{owner_repo}/actions/runners/generate-jitconfig"
    out = _github_post(path, token=inst_token, token_scheme="Bearer", body=body)
    # Return the plain encoded_jit_config. Compression to fit the 4096-char
    # runHookPayload limit happens in the orchestrator, which gzips the WHOLE
    # envelope ({jitConfig, callbackId, callbackUrl}) for the best ratio.
    return out["encoded_jit_config"]


def _callback_url(event) -> str:
    ctx = event.get("requestContext", {})
    domain = ctx.get("domainName")
    stage = ctx.get("stage")
    if domain and stage:
        return f"https://{domain}/{stage}/callback"
    return os.environ.get("CALLBACK_URL", "http://localhost/callback")


def handler(event, _context):
    # 1. Verify the signature over the raw body BEFORE parsing anything.
    if not _signature_valid(event):
        return _response(401, {"error": "invalid or missing X-Hub-Signature-256"})

    # 2. Parse.
    try:
        body = json.loads(_raw_body_bytes(event) or b"{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "invalid JSON body"})

    # 3. Filter: only newly queued workflow_job events carrying the required label.
    if body.get("action") and body["action"] != "queued":
        return _response(202, {"ignored": body.get("action")})

    job = body.get("workflow_job", {})
    labels = job.get("labels", []) or []
    if REQUIRED_RUNNER_LABEL not in labels:
        return _response(202, {"ignored": "missing required label", "label": REQUIRED_RUNNER_LABEL})

    repo = body.get("repository") or {}
    owner_repo = repo.get("full_name")  # "owner/repo"; falls back to org below
    org_login = (body.get("organization") or {}).get("login")
    target = owner_repo or org_login

    # 4. Mint the JIT config (GitHub App). If credentials are not yet configured,
    #    dispatch with an empty jitConfig so the pipeline still runs end to end
    #    (the runner reports NoJitConfig). This is the placeholder path.
    jit_config = ""
    runner_name = f"microvm-{job.get('id', 'runner')}"
    try:
        creds = _get_app_creds()
        if creds and target:
            jit_config = _mint_jit_config(creds, target, runner_name, labels)
        elif not creds:
            print("GitHub App credentials not configured; dispatching with empty jitConfig")
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError) as exc:
        # Do not echo exc (its body/message can carry token fragments). Log type.
        print(f"JIT mint failed: {type(exc).__name__}")
        return _response(502, {"error": "failed to mint JIT config"})

    github = {
        "runId": job.get("run_id"),
        "jobId": job.get("id"),
        "labels": labels,
        "repositoryUrl": repo.get("html_url"),
        "runnerName": runner_name,
        "jitConfig": jit_config,  # encoded_jit_config -> orchestrator runHookPayload
    }

    orchestrator_event = {"github": github, "callbackUrl": _callback_url(event)}

    lambda_client.invoke(
        FunctionName=ORCHESTRATOR_ALIAS_ARN,
        InvocationType="Event",
        Payload=json.dumps(orchestrator_event).encode(),
    )

    return _response(202, {"accepted": True, "jobId": github["jobId"]})

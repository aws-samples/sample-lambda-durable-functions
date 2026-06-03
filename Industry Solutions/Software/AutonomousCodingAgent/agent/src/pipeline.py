"""Coding pipeline: clone repo, invoke model, commit, push, create PR."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from enum import Enum

import boto3
import requests


class TaskStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


def _get_github_token() -> str:
    """Fetch GitHub token from Secrets Manager."""
    secret_arn = os.environ.get("GITHUB_TOKEN_SECRET_ARN", "")
    if not secret_arn:
        raise RuntimeError("No github_token provided and GITHUB_TOKEN_SECRET_ARN not set")
    sm = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    resp = sm.get_secret_value(SecretId=secret_arn)
    return resp["SecretString"]


def _send_callback(callback_id: str, function_name: str, result: dict) -> None:
    """Signal the durable orchestrator that the task is complete via SendDurableExecutionCallbackSuccess."""
    if not callback_id or not function_name:
        return
    lambda_client = boto3.client("lambda", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    try:
        lambda_client.send_durable_execution_callback_success(
            CallbackId=callback_id,
            Result=json.dumps(result),
        )
        print(f"[pipeline] Callback sent: {callback_id}")
    except Exception as e:
        print(f"[pipeline] Callback send failed: {e}")


def run_coding_task(
    repo_url: str,
    task_description: str,
    github_token: str = "",
    model_id: str = "us.anthropic.claude-sonnet-4-6",
    max_turns: int = 50,
    branch_name: str = "",
    task_id: str = "",
    output_bucket: str = "",
    callback_id: str = "",
    function_name: str = "",
) -> dict:
    """Run the full coding pipeline. Returns dict with pr_url or error."""

    # Resolve GitHub token from Secrets Manager if not provided
    if not github_token and not output_bucket:
        try:
            github_token = _get_github_token()
        except Exception:
            pass

    # If no token and no bucket, check for OUTPUT_BUCKET env var
    if not github_token and not output_bucket:
        output_bucket = os.environ.get("OUTPUT_BUCKET", "")

    work_dir = tempfile.mkdtemp(prefix="agent-")

    try:
        # 1. Gather context (clone if we have a token, otherwise use task description only)
        if github_token:
            print(f"[pipeline] Cloning {repo_url}...")
            _run_git(["clone", "--depth=1", _auth_url(repo_url, github_token), work_dir])
            context = _gather_repo_context(work_dir)
        else:
            context = f"Repository: {repo_url}\n(No clone — running in S3 output mode)"

        # 2. Invoke Bedrock model to generate changes
        print(f"[pipeline] Invoking {model_id}...")
        changes = _invoke_model(model_id, task_description, context)

        if not changes:
            return {"error": "Model returned no changes", "task_id": task_id}

        # 3. Output to S3 or GitHub
        if output_bucket:
            # S3 output mode — write generated files to bucket for inspection
            s3_prefix = f"coding-agent/{task_id or 'test'}/"
            _upload_to_s3(output_bucket, s3_prefix, changes)
            s3_uri = f"s3://{output_bucket}/{s3_prefix}"
            print(f"[pipeline] Files written to {s3_uri}")
            result = {"status": "completed", "s3_uri": s3_uri, "task_id": task_id, "files": [c["path"] for c in changes]}
            _send_callback(callback_id, function_name, result)
            return result

        # GitHub mode — apply, commit, push (PR created by orchestrator)
        if not branch_name:
            branch_name = f"agent/{task_id or 'task'}"
        _run_git(["checkout", "-b", branch_name], cwd=work_dir)
        _apply_changes(work_dir, changes)
        _run_git(["add", "-A"], cwd=work_dir)
        _run_git(["commit", "-m", f"feat: {task_description[:72]}"], cwd=work_dir)
        _run_git(["push", "origin", branch_name], cwd=work_dir)

        print(f"[pipeline] Branch pushed: {branch_name}")
        result = {"status": "completed", "branch_name": branch_name, "task_id": task_id, "files": [c["path"] for c in changes]}
        _send_callback(callback_id, function_name, result)
        return result

    except Exception as e:
        print(f"[pipeline] Failed: {e}")
        result = {"error": str(e), "task_id": task_id}
        _send_callback(callback_id, function_name, result)
        return result


def _auth_url(repo_url: str, token: str) -> str:
    """Inject token into HTTPS clone URL."""
    # repo_url: https://github.com/owner/repo.git
    return repo_url.replace("https://", f"https://x-access-token:{token}@")


def _run_git(args: list[str], cwd: str | None = None) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _gather_repo_context(work_dir: str) -> str:
    """Read README and top-level file listing for model context."""
    parts = []

    # File tree (top-level)
    files = os.listdir(work_dir)
    files = [f for f in files if not f.startswith(".")]
    parts.append(f"Files in repo root: {', '.join(sorted(files))}")

    # README
    for name in ("README.md", "readme.md", "README"):
        path = os.path.join(work_dir, name)
        if os.path.isfile(path):
            with open(path, "r") as f:
                content = f.read(4000)
            parts.append(f"README:\n{content}")
            break

    return "\n\n".join(parts)


def _invoke_model(model_id: str, task: str, context: str) -> list[dict]:
    """Call Bedrock to generate file changes. Returns list of {path, content}."""
    from botocore.config import Config as BotoConfig
    bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"),
                          config=BotoConfig(read_timeout=300))

    system_prompt = """You are a coding agent. Given a task and repository context, generate file changes.
Respond with a JSON array of objects, each with "path" (relative file path) and "content" (full file content).
Only output the JSON array, no markdown fences or explanation."""

    user_prompt = f"""Repository context:
{context}

Task: {task}

Generate the file changes as a JSON array of {{"path": "...", "content": "..."}} objects."""

    response = bedrock.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        system=[{"text": system_prompt}],
        inferenceConfig={"maxTokens": 65536, "temperature": 0.2},
    )

    text = response["output"]["message"]["content"][0]["text"]

    # Parse JSON from response (handle markdown fences if present)
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]

    # Try direct parse first; if it fails, fix unescaped newlines in string values
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Model sometimes outputs literal newlines inside JSON strings.
        # Fix by finding content between quotes and escaping newlines.
        import re
        fixed = re.sub(
            r'("content"\s*:\s*")(.*?)("(?:\s*[,}\]]))',
            lambda m: m.group(1) + m.group(2).replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t') + m.group(3),
            text,
            flags=re.DOTALL,
        )
        return json.loads(fixed)


def _apply_changes(work_dir: str, changes: list[dict]) -> None:
    """Write file changes to the working directory."""
    for change in changes:
        path = os.path.join(work_dir, change["path"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(change["content"])
        print(f"[pipeline] Wrote: {change['path']}")


def _upload_to_s3(bucket: str, prefix: str, changes: list[dict]) -> None:
    """Upload generated files to S3 for inspection."""
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    for change in changes:
        key = prefix + change["path"]
        s3.put_object(Bucket=bucket, Key=key, Body=change["content"].encode("utf-8"))
        print(f"[pipeline] Uploaded: s3://{bucket}/{key}")


def _create_pr(repo_url: str, branch: str, title: str, token: str) -> str:
    """Create a GitHub pull request. Returns the PR URL."""
    # Extract owner/repo from URL
    # https://github.com/owner/repo.git -> owner/repo
    parts = repo_url.rstrip("/").removesuffix(".git").split("/")
    owner, repo = parts[-2], parts[-1]

    response = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        },
        json={
            "title": f"feat: {title[:72]}",
            "body": f"Automated PR created by coding agent.\n\n**Task:** {title}",
            "head": branch,
            "base": "main",
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["html_url"]

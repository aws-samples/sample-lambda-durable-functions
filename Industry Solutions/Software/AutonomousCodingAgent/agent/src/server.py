"""AgentCore Runtime server.

Exposes /ping (GET) and /invocations (POST) on port 8080.
Matches the AgentCore Runtime container contract.
"""

import threading
import time
from datetime import UTC, datetime

import boto3
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from pipeline import run_coding_task, TaskStatus

app = FastAPI(title="Coding Agent Runtime")

# Track background task state
_task_lock = threading.Lock()
_task_thread: threading.Thread | None = None
_task_result: dict | None = None
_task_status: TaskStatus = TaskStatus.IDLE


class InvocationRequest(BaseModel):
    input: dict


@app.get("/ping")
async def ping():
    """Health check. Returns HealthyBusy while a task is running."""
    with _task_lock:
        if _task_thread and _task_thread.is_alive():
            return {"status": "HealthyBusy"}
    return {"status": "healthy"}


@app.post("/invocations")
async def invocations(request: Request, body: InvocationRequest):
    """Accept a task or return status of the running task."""
    global _task_thread, _task_result, _task_status

    inp = body.input
    action = inp.get("action")

    # Status poll from orchestrator
    if action == "status":
        with _task_lock:
            if _task_status == TaskStatus.COMPLETED:
                return JSONResponse(content={
                    "output": {
                        "result": {
                            "status": "completed",
                            "pr_url": (_task_result or {}).get("pr_url"),
                        },
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                })
            elif _task_status == TaskStatus.FAILED:
                return JSONResponse(content={
                    "output": {
                        "result": {
                            "status": "error",
                            "error": (_task_result or {}).get("error", "Unknown error"),
                        },
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                })
            else:
                return JSONResponse(content={
                    "output": {
                        "result": {"status": "running"},
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                })

    # New task submission
    with _task_lock:
        if _task_thread and _task_thread.is_alive():
            return JSONResponse(status_code=409, content={
                "output": {"result": {"status": "busy", "error": "A task is already running"}}
            })
        _task_status = TaskStatus.RUNNING
        _task_result = None

    def _run():
        global _task_result, _task_status
        try:
            result = run_coding_task(
                repo_url=inp.get("repo_url", ""),
                task_description=inp.get("task_description", ""),
                github_token=inp.get("github_token", ""),
                model_id=inp.get("model_id", "us.anthropic.claude-sonnet-4-6"),
                max_turns=int(inp.get("max_turns", 50)),
                branch_name=inp.get("branch_name", ""),
                task_id=inp.get("task_id", ""),
                output_bucket=inp.get("output_bucket", ""),
                callback_id=inp.get("callback_id", ""),
                function_name=inp.get("function_name", ""),
            )
            with _task_lock:
                _task_result = result
                _task_status = TaskStatus.COMPLETED if result.get("pr_url") else TaskStatus.FAILED
        except Exception as e:
            with _task_lock:
                _task_result = {"error": str(e)}
                _task_status = TaskStatus.FAILED

    _task_thread = threading.Thread(target=_run, name="coding-task", daemon=True)
    _task_thread.start()

    task_id = inp.get("task_id", "unknown")
    return JSONResponse(content={
        "output": {
            "message": {"role": "assistant", "content": [{"text": f"Task accepted: {task_id}"}]},
            "result": {"status": "accepted", "task_id": task_id},
            "timestamp": datetime.now(UTC).isoformat(),
        }
    })

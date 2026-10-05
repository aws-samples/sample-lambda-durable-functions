#!/usr/bin/env python3
"""MicroVM runner app for an ephemeral GitHub self-hosted runner (no-docker).

Wires the MicroVM lifecycle to this project's durable-callback orchestration.
HTTP server on RUNNER_PORT (default 8080):

  Image-build hooks (called while building the image):
    GET  /ready     -> 200 once the app has booted (so the snapshot is complete)
    POST /validate  -> 200 to confirm the snapshot is good (warm the hot path)

  Runtime hooks (called by the platform on the running VM):
    POST /run       -> fired once after RunMicrovm; carries runHookPayload with
                       the GitHub JIT config (encoded_jit_config)
    POST /suspend, /resume, /terminate -> fast notifications

  Orchestrator channel (this project's durable-callback design):
    POST /configure -> the durable orchestrator delivers {callbackId, callbackUrl}

Flow: /run captures and decodes the JIT config. /configure delivers the callback
coordinates and starts the runner exactly once via entrypoint.sh (which execs
`./run.sh --jitconfig`). The runner registers as a JIT self-hosted runner, runs
one job, auto-deregisters, and exits. On exit we POST the result to callbackUrl,
which resumes the suspended durable function and triggers TerminateMicrovm.

JIT config handling mirrors GitHub's just-in-time runner sample: the run-hook
payload is either the plain base64 encoded_jit_config, or base64(gzip(...)); we
detect the gzip magic bytes after the outer base64 decode and decompress.
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("RUNNER_PORT", "8080"))
RUNNER_DIR = "/actions-runner"

# jit_config: the decoded encoded_jit_config string to pass to run.sh --jitconfig.
_state: dict = {
    "jit_config": None,
    "runner_name": None,
    "callbackId": None,
    "callbackUrl": None,
    "started": False,
}
_lock = threading.Lock()


def _decode_envelope(raw: str) -> dict:
    """Decode the runHookPayload envelope into a dict.

    The orchestrator sends base64(gzip(JSON {jitConfig, callbackId, callbackUrl}))
    to stay under the 4096-char runHookPayload limit. Detect gzip magic bytes
    after the outer base64 decode and gunzip; fall back to treating the input as
    plain JSON for forward/backward compatibility.
    """
    try:
        buf = base64.b64decode(raw)
        if len(buf) >= 2 and buf[0] == 0x1F and buf[1] == 0x8B:
            return json.loads(gzip.decompress(buf).decode("utf-8"))
    except Exception:  # noqa: BLE001 - fall through to plain JSON
        pass
    return json.loads(raw)


def _decode_jit_config(payload: str) -> str:
    """Return the encoded_jit_config, transparently handling base64(gzip(...)).

    GitHub's generate-jitconfig returns a base64 `encoded_jit_config`. Senders
    may additionally gzip+base64 it to shrink the run-hook payload; detect the
    gzip magic bytes (0x1f 0x8b) after the outer base64 decode and decompress.
    """
    buf = base64.b64decode(payload)
    if len(buf) >= 2 and buf[0] == 0x1F and buf[1] == 0x8B:
        return gzip.decompress(buf).decode("utf-8")
    # Not gzipped: the payload itself is the encoded_jit_config (base64 string).
    return payload


def _runner_name_from_jit(encoded_jit_config: str) -> str | None:
    """Derive the runner's AgentName from the nested JIT config, for logging."""
    try:
        outer = json.loads(base64.b64decode(encoded_jit_config).decode("utf-8"))
        runner = json.loads(base64.b64decode(outer[".runner"]).decode("utf-8"))
        return runner.get("AgentName")
    except Exception:  # noqa: BLE001 - name is best-effort, for logs only
        return None


def _send_callback(status: str, result: dict | None = None, error: dict | None = None) -> None:
    url = _state.get("callbackUrl")
    cb_id = _state.get("callbackId")
    if not url or not cb_id:
        print("No callback configured; cannot report completion", flush=True)
        return
    body = {"callbackId": cb_id, "status": status}
    if result is not None:
        body["result"] = result
    if error is not None:
        body["error"] = error
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            print(f"Callback {status} -> HTTP {resp.status}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"Callback POST failed: {exc}", flush=True)


def _run_github_job() -> None:
    """Launch the runner once via entrypoint.sh, then report the result back."""
    jit = _state.get("jit_config")
    if not jit:
        _send_callback("failure", error={"type": "NoJitConfig",
                                         "message": "no JIT config in run-hook payload"})
        return
    print(f"Starting GitHub runner {_state.get('runner_name') or '(unknown)'}", flush=True)
    try:
        proc = subprocess.run(
            ["./entrypoint.sh"],
            cwd=RUNNER_DIR,
            env={**os.environ, "ENCODED_JIT_CONFIG": jit},
            check=False,
        )
        if proc.returncode == 0:
            _send_callback("success", result={"conclusion": "success"})
        else:
            _send_callback("failure", error={"type": "RunnerExit",
                                             "message": f"run.sh exited {proc.returncode}"})
    except Exception as exc:  # noqa: BLE001
        # Do not echo exc directly: a failure near the JIT config could leak the
        # registration credential. Log the type only.
        print(f"Runner launch error: {type(exc).__name__}", flush=True)
        _send_callback("failure", error={"type": "RunnerError", "message": "runner launch failed"})


class Handler(BaseHTTPRequestHandler):
    # The Lambda MicroVMs platform calls hooks under this path prefix.
    HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1"

    def _ok(self, code=200, body=b"OK"):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> dict:
        try:
            return json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError:
            return {}

    def _hook(self, name: str) -> str:
        return f"{self.HOOK_PREFIX}/{name}"

    def do_GET(self):  # noqa: N802
        # The platform POSTs /ready, but answer GET too for health probes.
        if self.path in (self._hook("ready"), "/ready"):
            self._ok()
        else:
            self._ok(404, b'{"error":"not found"}')

    def do_POST(self):  # noqa: N802
        path = self.path
        # Build hooks: ready + validate (platform POSTs both).
        if path in (self._hook("ready"), self._hook("validate")):
            self._ok()
            return
        if path == self._hook("run"):
            # /run carries only the small callback coordinates (runHookPayload is
            # capped at 4096). The large JIT config arrives via /configure. We do
            # NOT start the job here — we wait until /configure delivers the JIT.
            payload = self._read_json()
            raw = payload.get("runHookPayload")
            try:
                if raw:
                    env = json.loads(raw)
                    with _lock:
                        _state["callbackId"] = env.get("callbackId")
                        _state["callbackUrl"] = env.get("callbackUrl")
                self._ok()
            except Exception as exc:  # noqa: BLE001
                print(f"Error handling /run: {type(exc).__name__}", flush=True)
                self._ok(500, b'{"error":"bad run payload"}')
            return
        if path in (self._hook("suspend"), self._hook("resume"), self._hook("terminate")):
            self._ok()
            return
        if path == "/configure":
            # Orchestrator delivers the JIT config here (no size limit on ingress).
            # Decode it, then start the one job in the background exactly once.
            payload = self._read_json()
            try:
                jit = _decode_jit_config(payload.get("jitConfig", ""))
                with _lock:
                    _state["jit_config"] = jit
                    _state["runner_name"] = _runner_name_from_jit(jit)
                    already = _state["started"]
                    _state["started"] = True
                if not already and jit:
                    threading.Thread(target=_run_github_job, daemon=True).start()
                self._ok(202, b'{"accepted":true}')
            except Exception as exc:  # noqa: BLE001 - never log raw (holds credential)
                print(f"Error handling /configure: {type(exc).__name__}", flush=True)
                self._ok(500, b'{"error":"bad configure payload"}')
            return
        self._ok(404, b'{"error":"not found"}')

    def log_message(self, fmt, *args):  # quieter logs
        print("runner: " + (fmt % args), flush=True)


def main():
    print(f"Runner app listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

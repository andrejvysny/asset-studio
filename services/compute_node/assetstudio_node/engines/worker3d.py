"""Client for the GPU1 3D worker (TRELLIS.2 sampling + GLB export) as recoverable executions.

The Studio chooses the execution id and persists it BEFORE submitting. Any uncertain outcome (timeout, reset, lost
response) is reconciled by looking the id up; a request is only (re)submitted when the worker has never seen it.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from .base import EngineUnavailable, ExecutionCancelled, ExecutionFailed, ExecutionLost, post_ack

TIMEOUT = httpx.Timeout(120.0, connect=5.0)
POLL_S = 1.0
MAX_WAIT_S = 3 * 3600.0


class Worker3dClient:
    name = "worker3d"
    simulated = False

    def __init__(self, base_url: str, client: httpx.Client | None = None, poll_s: float = POLL_S) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = client or httpx.Client(base_url=self.base_url, timeout=TIMEOUT)
        self.poll_s = poll_s

    def health(self) -> dict[str, Any]:
        try:
            r = self.http.get("/health", timeout=5.0)
            body = r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return {"reachable": False}
        return {"reachable": r.status_code == 200, **body}

    def lease(self, epoch: int) -> dict[str, Any]:
        return post_ack(self.http, "/lease", {"epoch": epoch}, 30.0)

    def unload(self, owner_token: str, epoch: int) -> dict[str, Any]:
        """Validated by the GPU lane. The worker drains ALL GPU work (sampling and export) before acking."""
        return post_ack(self.http, "/unload", {"owner_token": owner_token, "epoch": epoch}, 630.0)

    # --- executions --------------------------------------------------------------------------------------------
    def status(self, execution_id: str) -> dict[str, Any] | None:
        try:
            r = self.http.get(f"/executions/{execution_id}")
        except httpx.HTTPError as e:
            raise EngineUnavailable(f"worker3d status: {type(e).__name__}") from e
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise EngineUnavailable(f"worker3d status: HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            raise EngineUnavailable("worker3d status: malformed body") from e
        if not isinstance(body, dict) or "state" not in body:
            raise EngineUnavailable("worker3d status: malformed body")
        return body

    def _submit(self, execution_id: str, op: str, params: dict[str, Any], body: bytes, epoch: int) -> None:
        try:
            r = self.http.post(f"/executions/{execution_id}", params={"op": op}, content=body,
                               headers={"x-lease-epoch": str(epoch), "x-exec-params": json.dumps(params),
                                        "content-type": "application/octet-stream"})
        except httpx.HTTPError as e:
            # Uncertain: it may have been admitted. The caller reconciles by status before any resubmission.
            raise EngineUnavailable(f"worker3d submit: {type(e).__name__}") from e
        if r.status_code in (400, 413, 422):
            raise ExecutionFailed(f"worker3d {op}: {r.text[:300]}", "input_invalid")
        if r.status_code == 409:
            raise EngineUnavailable(f"worker3d {op}: {r.text[:200]}")  # stale lease or id conflict
        if r.status_code not in (200, 202):
            raise EngineUnavailable(f"worker3d {op}: HTTP {r.status_code} {r.text[:200]}")

    def result(self, execution_id: str, expected_sha256: str | None) -> tuple[bytes, dict[str, Any]]:
        try:
            r = self.http.get(f"/executions/{execution_id}/result")
        except httpx.HTTPError as e:
            raise EngineUnavailable(f"worker3d result: {type(e).__name__}") from e
        if r.status_code != 200 or not r.content:
            raise EngineUnavailable(f"worker3d result: HTTP {r.status_code}")
        if expected_sha256 and hashlib.sha256(r.content).hexdigest() != expected_sha256:
            raise EngineUnavailable("worker3d result: bytes do not match the recorded digest")
        try:
            meta = json.loads(r.headers.get("x-worker-meta", "{}"))
        except ValueError as e:
            raise EngineUnavailable("worker3d result: malformed metadata") from e
        return r.content, meta if isinstance(meta, dict) else {}

    def cancel(self, execution_id: str) -> None:
        try:
            self.http.post(f"/executions/{execution_id}/cancel")
        except httpx.HTTPError:
            pass  # a timed-out cancel is not proof of anything; the status poll decides

    def ack(self, execution_id: str) -> None:
        """Called only after the Studio durably stored the result. Failure is harmless (re-fetch is idempotent)."""
        try:
            self.http.delete(f"/executions/{execution_id}")
        except httpx.HTTPError:
            pass

    def execute(self, execution_id: str, op: str, params: dict[str, Any], body: bytes, *, epoch: int,
                should_cancel: Callable[[], bool] | None = None) -> tuple[bytes, dict[str, Any]]:
        """Reconcile -> (submit if never seen) -> poll -> verified result. Raises ExecutionFailed / ExecutionLost /
        ExecutionCancelled / EngineUnavailable (uncertain: retry later with the SAME id)."""
        st = self.status(execution_id)
        if st is None:
            self._submit(execution_id, op, params, body, epoch)
        deadline = time.monotonic() + MAX_WAIT_S
        while True:
            st = self.status(execution_id)
            if st is None:
                raise EngineUnavailable("worker3d forgot an admitted execution")
            state = st["state"]
            if state == "succeeded":
                data, meta = self.result(execution_id, st.get("result_sha256"))
                return data, {**meta, "execution_id": execution_id, "worker_session": st.get("session_id")}
            if state == "failed":
                raise ExecutionFailed(str(st.get("error") or "failed"), str(st.get("code") or "internal"))
            if state == "lost":
                raise ExecutionLost(str(st.get("error") or "worker restarted"))
            if state == "cancelled":
                raise ExecutionCancelled()
            if should_cancel is not None and should_cancel():
                self.cancel(execution_id)
                raise ExecutionCancelled()
            if time.monotonic() > deadline:
                raise EngineUnavailable("worker3d execution exceeded the wait budget")
            time.sleep(self.poll_s)


"""Fake aux / worker3d engine HTTP server, run as its own OS process by the process-level harness.

It speaks the wire surface the runner's engine clients use (aux: /health /lease /unload /enhance ...; worker3d:
/health /lease /unload only) with the REAL lease semantics (services/worker_common/lease.py, imported, not copied).
Nothing is simulated about fencing or drain: a request counts as active from admission to completion, and /unload
refuses to acknowledge while one is running.

Test control surface (not part of the product wire format):
  POST /_control {"delay_s": float, "hold": bool}  per-request delay; `hold` parks requests until released
  GET  /_log                                         every admitted request (epoch, start/end) + lease info

Run: uv run python tests/process/fake_engines.py --port N --kind aux|worker3d [--delay-s S] [--drain-timeout S]
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "worker_common"))

import uvicorn  # noqa: E402
from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from lease import Lease, StaleLease  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

HOLD_LIMIT_S = 120.0  # a leaked hold must not wedge a test run forever


class Control:
    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.hold = False
        self.cond = threading.Condition()
        self.log: list[dict[str, Any]] = []

    def begin(self, path: str, epoch: int | None, execution_id: str | None) -> dict[str, Any]:
        with self.cond:
            row = {"n": len(self.log), "path": path, "epoch": epoch, "execution_id": execution_id,
                   "started_at": time.time(), "ended_at": None}
            self.log.append(row)
            return row

    def wait(self) -> None:
        with self.cond:
            self.cond.wait_for(lambda: not self.hold, timeout=HOLD_LIMIT_S)
            delay = self.delay_s
        if delay > 0:
            time.sleep(delay)

    def end(self, row: dict[str, Any]) -> None:
        with self.cond:
            row["ended_at"] = time.time()

    def set(self, delay_s: float | None, hold: bool | None) -> None:
        with self.cond:
            if delay_s is not None:
                self.delay_s = delay_s
            if hold is not None:
                self.hold = hold
            self.cond.notify_all()


class LeaseReq(BaseModel):
    epoch: int = Field(ge=1)


class UnloadReq(BaseModel):
    owner_token: str = Field(min_length=1, max_length=100)
    epoch: int = Field(ge=0)


class ControlReq(BaseModel):
    delay_s: float | None = Field(default=None, ge=0)
    hold: bool | None = None


def make_app(kind: str, delay_s: float, drain_timeout: float) -> FastAPI:
    app = FastAPI(title=f"fake {kind}")
    lease, ctl = Lease(), Control(delay_s)

    def gpu_work(x_lease_epoch: str | None = Header(default=None),
                 x_execution_id: str | None = Header(default=None, max_length=80)):  # noqa: ANN202
        epoch = int(x_lease_epoch) if x_lease_epoch and x_lease_epoch.isdigit() else None
        try:
            lease.enter(epoch)
        except StaleLease as e:
            raise HTTPException(409, f"stale_lease: {e}") from e
        try:
            yield epoch, x_execution_id
        finally:
            lease.leave()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "simulated": True, "kind": kind, "lease": lease.info()}

    @app.post("/lease")
    def grant(req: LeaseReq) -> dict[str, Any]:
        try:
            return lease.grant(req.epoch)
        except StaleLease as e:
            raise HTTPException(409, f"stale_lease: {e}") from e

    @app.post("/unload")
    def unload(req: UnloadReq) -> dict[str, Any]:
        try:
            drained = lease.drain(req.epoch, timeout=drain_timeout)
        except StaleLease as e:
            raise HTTPException(409, f"stale_lease: {e}") from e
        if not drained:
            raise HTTPException(409, "GPU work still active")
        return {"loaded": False, "owner_token": req.owner_token, **lease.info()}

    @app.get("/_log")
    def get_log() -> dict[str, Any]:
        with ctl.cond:
            return {"requests": [dict(r) for r in ctl.log], "lease": lease.info(), "delay_s": ctl.delay_s,
                    "hold": ctl.hold}

    @app.post("/_control")
    def control(req: ControlReq) -> dict[str, Any]:
        ctl.set(req.delay_s, req.hold)
        return {"delay_s": ctl.delay_s, "hold": ctl.hold}

    if kind == "aux":
        @app.post("/enhance")
        def enhance(body: dict[str, Any], ctx: tuple[int | None, str | None] = Depends(gpu_work)) -> dict[str, Any]:
            epoch, execution_id = ctx
            row = ctl.begin("/enhance", epoch, execution_id)
            try:
                ctl.wait()
            finally:
                ctl.end(row)
            text = str(body.get("brief", "")).strip().rstrip(".")
            return {"description": f"{text[:1].upper()}{text[1:]}, clearly readable form, process-harness enhancement.",
                    "short_title": text[:30], "tags": [str(body.get("kind", ""))], "facts": [text] if text else [],
                    "additions": [], "assumptions": [], "reference_cues": [],
                    "meta": {"model": "fake-process", "simulated": True, "seconds": ctl.delay_s, "raw": "fake",
                             "execution_id": execution_id}}
    return app


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--kind", choices=("aux", "worker3d"), required=True)
    p.add_argument("--delay-s", type=float, default=0.0)
    p.add_argument("--drain-timeout", type=float, default=1.0, help="how long /unload waits for active requests")
    a = p.parse_args()
    uvicorn.run(make_app(a.kind, a.delay_s, a.drain_timeout), host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()

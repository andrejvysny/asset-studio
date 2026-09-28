#!/usr/bin/env python3
"""Drive a Line A job through the ComfyUI API (v1 API): enhance -> generate+QA -> select -> 3D.

Selection stays manual: without --select the script stops after QA and prints the command to continue.
  ./scripts/run_job.py new "wooden medieval barrel" --asset-type small_prop --triangles 500
  ./scripts/run_job.py select <job_id> 2
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
WF_DIR = ROOT / "config" / "workflows"


class Comfy:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self.client_id = uuid.uuid4().hex

    def _req(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            sys.exit(f"HTTP {e.code} {path}: {e.read().decode()[:3000]}")

    def run(self, workflow: dict, timeout_s: float = 3600) -> dict:
        pid = self._req("POST", "/prompt", {"prompt": workflow, "client_id": self.client_id})["prompt_id"]
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            hist = self._req("GET", f"/history/{pid}").get(pid)
            status = (hist or {}).get("status", {})
            if status.get("status_str") == "error":
                msgs = [m for m in status.get("messages", []) if m[0] == "execution_error"]
                detail = msgs[0][1] if msgs else status
                sys.exit(f"workflow failed: {json.dumps(detail, indent=2)[:4000]}")
            if status.get("completed"):
                return hist["outputs"]
            time.sleep(2)
        sys.exit(f"timeout waiting for {pid}")

    def job(self, job_id: str) -> dict:
        return self._req("GET", f"/line_a/jobs/{job_id}")


def load_wf(name: str, **by_title: dict[str, Any]) -> dict:
    wf = copy.deepcopy(json.loads((WF_DIR / f"{name}.api.json").read_text()))
    titles = {node["_meta"]["title"]: node for node in wf.values()}
    for title, values in by_title.items():
        titles[title]["inputs"].update(values)
    return wf


def cmd_new(c: Comfy, a: argparse.Namespace) -> None:
    final = Path(a.final_prompt_file).read_text() if a.final_prompt_file else ""
    wf = load_wf("line_a_new_asset",
                 create_job={"prompt": a.prompt, "asset_type": a.asset_type, "target_triangles": a.triangles,
                             "candidate_count": a.count, "seed_family": a.seed, "lora_name": a.lora or "none",
                             "lora_strength": a.lora_strength},
                 confirm_prompt={"final_prompt": final},
                 generate={"speed_preset": a.speed})
    t0 = time.monotonic()
    out = c.run(wf)
    # gallery node (12) outputs images under line_a/<job_id>/
    job_id = out["12"]["images"][0]["subfolder"].split("/", 1)[1]
    job = c.job(job_id)
    print(f"job: {job_id} ({time.monotonic() - t0:.0f}s)")
    for name, info in (job["qa"] or {}).get("candidates", {}).items():
        print(f"  {name}: {info['status']}  {'; '.join(info['reasons'][:2])}")
    if a.select is None:
        print(f"Review in ComfyUI ('Line A - 2 Review & Approve') or:\n  ./scripts/run_job.py select {job_id} <index>")
        return
    cmd_select(c, argparse.Namespace(job_id=job_id, index=a.select, seed=42, pipeline_type=a.pipeline_type))


def cmd_select(c: Comfy, a: argparse.Namespace) -> None:
    t0 = time.monotonic()
    c.run(load_wf("line_a_review_approve", gallery={"job_id": a.job_id},
                  approve={"approve_candidate": f"{a.index:02d}"},
                  trellis={"model_seed": a.seed, "pipeline_type": a.pipeline_type}))
    job = c.job(a.job_id)
    print(f"3D ({time.monotonic() - t0:.0f}s) state: {job['state']}")
    print(f"mesh: {json.dumps(job['manifest'].get('mesh'), indent=2)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8188")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("prompt")
    n.add_argument("--asset-type", default="none")
    n.add_argument("--triangles", type=int, default=0)
    n.add_argument("--count", type=int, default=4)
    n.add_argument("--seed", type=int, default=0)
    n.add_argument("--lora", default=None)
    n.add_argument("--lora-strength", type=float, default=0.8)
    n.add_argument("--speed", default="quality", choices=["quality", "lightning_8step", "lightning_4step"])
    n.add_argument("--final-prompt-file", help="edited prompt; default = enhanced as-is")
    n.add_argument("--select", type=int, help="candidate index to continue with (skips manual pause)")
    n.add_argument("--pipeline-type", default="1024_cascade")
    s = sub.add_parser("select")
    s.add_argument("job_id")
    s.add_argument("index", type=int)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--pipeline-type", default="1024_cascade")
    a = ap.parse_args()
    c = Comfy(a.url)
    cmd_new(c, a) if a.cmd == "new" else cmd_select(c, a)


if __name__ == "__main__":
    main()

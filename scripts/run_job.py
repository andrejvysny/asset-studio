#!/usr/bin/env python3
"""Drive Line A jobs through the ComfyUI API (the v1 API). Each human decision is a separate command:

  python3 scripts/run_job.py new "wooden medieval barrel" --asset-type small_prop --triangles 500
      -> job created + prompt enhanced; stops at prompt_enhanced (no images)
  python3 scripts/run_job.py confirm <job_id> [--edited-file desc.txt] [--speed lightning_8step]
      -> same job: effective prompt = description + model-sheet template; 4 candidates + QA
  python3 scripts/run_job.py approve <job_id> <index>
      -> binds approval to the reviewed candidate set + image sha256, then runs cut-out + TRELLIS
  python3 scripts/run_job.py reexport <job_id> <from_attempt> [--remesh] [--drop-floaters]
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

    def req(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            sys.exit(f"HTTP {e.code} {path}: {e.read().decode()[:3000]}")

    def run(self, workflow: dict, timeout_s: float = 3600) -> dict:
        pid = self.req("POST", "/prompt", {"prompt": workflow, "client_id": self.client_id})["prompt_id"]
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            hist = self.req("GET", f"/history/{pid}").get(pid)
            status = (hist or {}).get("status", {})
            if status.get("status_str") == "error":
                msgs = [m for m in status.get("messages", []) if m[0] == "execution_error"]
                sys.exit(f"workflow failed: {json.dumps(msgs[0][1] if msgs else status, indent=2)[:4000]}")
            if status.get("completed"):
                return hist["outputs"]
            time.sleep(2)
        sys.exit(f"timeout waiting for {pid}")

    def job(self, job_id: str) -> dict:
        return self.req("GET", f"/line_a/jobs/{job_id}")


def load_wf(name: str, **by_title: dict[str, Any]) -> dict:
    wf = copy.deepcopy(json.loads((WF_DIR / f"{name}.api.json").read_text()))
    titles = {node["_meta"]["title"]: node for node in wf.values()}
    for title, values in by_title.items():
        titles[title]["inputs"].update(values)
    return wf


def node_output(outputs: dict, wf: dict, title: str, key: str = "text") -> list:
    node_id = next(k for k, v in wf.items() if v["_meta"]["title"] == title)
    return outputs.get(node_id, {}).get(key, [])


def cmd_new(c: Comfy, a: argparse.Namespace) -> None:
    wf = load_wf("line_a_enhance", create_job={
        "prompt": a.prompt, "asset_type": a.asset_type, "target_triangles": a.triangles, "candidate_count": a.count,
        "seed_family": a.seed, "lora_name": a.lora or "none", "lora_strength": a.lora_strength})
    out = c.run(wf)
    job_id = node_output(out, wf, "create_job")[0]
    print(f"job: {job_id}\nenhanced description:\n  {node_output(out, wf, 'enhance')[0]}\n")
    print(f"edit it into a file if you like, then:\n  python3 scripts/run_job.py confirm {job_id} [--edited-file f.txt]")


def cmd_confirm(c: Comfy, a: argparse.Namespace) -> None:
    edited = Path(a.edited_file).read_text() if a.edited_file else ""
    t0 = time.monotonic()
    c.run(load_wf("line_a_generate", confirm_prompt={"job_id": a.job_id, "edited_description": edited},
                  generate={"speed_preset": a.speed}))
    job = c.job(a.job_id)
    print(f"candidates + QA ({time.monotonic() - t0:.0f}s), set {job['candidate_set']['set_id']}:")
    for name, qa in job["qa"].items():
        cov = qa["coverage"]
        print(f"  {name}: {qa['status']} ({cov['ran']}/{cov['total']})  {'; '.join(qa['reasons'][:2])}")
    print(f"review the images in output/{a.job_id}/candidates, then:\n  python3 scripts/run_job.py approve {a.job_id} <index>")


def cmd_approve(c: Comfy, a: argparse.Namespace) -> None:
    job = c.job(a.job_id)
    cset = job["candidate_set"]
    name = f"{a.index:02d}"
    res = c.req("POST", f"/line_a/jobs/{a.job_id}/approve",
                {"set_id": cset["set_id"], "index": a.index, "image_sha256": cset["images"][name], "override": a.override})
    attempt = res["attempt"]
    print(f"approved {name} -> {attempt['id']} ({'new' if res['created'] else 'existing'} attempt)")
    t0 = time.monotonic()
    c.run(load_wf("line_a_3d", cutout={"job_id": a.job_id, "attempt_id": attempt["id"]},
                  trellis={"model_seed": a.seed, "pipeline_type": a.pipeline_type}))
    _print_attempt(c, a.job_id, attempt["id"], time.monotonic() - t0)


def cmd_reexport(c: Comfy, a: argparse.Namespace) -> None:
    wf = load_wf("line_a_reexport", reexport={"job_id": a.job_id, "from_attempt": a.from_attempt,
                                              "remesh": a.remesh, "drop_floaters": a.drop_floaters})
    t0 = time.monotonic()
    c.run(wf)
    _print_attempt(c, a.job_id, c.job(a.job_id)["current_attempt"], time.monotonic() - t0)


def _print_attempt(c: Comfy, job_id: str, attempt_id: str, secs: float) -> None:
    job = c.job(job_id)
    att = next(x for x in job["attempts"] if x["id"] == attempt_id)
    print(f"{attempt_id}: {att['state']} ({secs:.0f}s) validated={att.get('validated')}")
    if att.get("mesh"):
        print(f"  triangles {att['mesh']['triangles']}  {att.get('triangles')}")
    if att.get("error"):
        print(f"  error: {att['error'][:500]}")


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
    c = sub.add_parser("confirm")
    c.add_argument("job_id")
    c.add_argument("--edited-file", help="edited description; default = enhanced as-is")
    c.add_argument("--speed", default="quality", choices=["quality", "lightning_8step", "lightning_4step"])
    p = sub.add_parser("approve")
    p.add_argument("job_id")
    p.add_argument("index", type=int)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--pipeline-type", default="1024_cascade")
    p.add_argument("--override", action="store_true", help="approve even if QA marked it not recommended/unverified")
    r = sub.add_parser("reexport")
    r.add_argument("job_id")
    r.add_argument("from_attempt")
    r.add_argument("--remesh", action="store_true")
    r.add_argument("--drop-floaters", action="store_true")
    a = ap.parse_args()
    {"new": cmd_new, "confirm": cmd_confirm, "approve": cmd_approve, "reexport": cmd_reexport}[a.cmd](Comfy(a.url), a)


if __name__ == "__main__":
    main()

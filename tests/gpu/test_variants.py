"""STRICT real-stack acceptance of asset variants (release gate for the 'experimental' generative variant route).

Runs against the LIVE stack and an EXISTING project (env VARIANT_PROJECT, default the owner's 'Demo 3D'): accepted
variants become real family members there; sources are never modified (only a family attachment). Evidence goes to
tests/gpu/artifacts/variants/. Outcome problems are collected per scenario and asserted at the end, so one bad
candidate does not hide the rest of the evidence; infrastructure errors (failed/blocked task, timeout) raise at once.
Order: pine, crate, T2I (after edit passes), icon (edit after T2I: the model switches both ways), direct transform.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image, ImageDraw, ImageStat

pytestmark = pytest.mark.gpu
URL = os.environ.get("STUDIO_URL", "http://127.0.0.1:8190")
COMFY = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
PID = os.environ.get("VARIANT_PROJECT", "prj_pt8ayktxz18bs7jy")
H = {"x-assetstudio": "1"}
P, V2 = f"/api/v1/projects/{PID}", f"/api/v2/projects/{PID}"
OUT = Path(__file__).parent / "artifacts" / "variants"
RUN = uuid.uuid4().hex[:6]
T0 = datetime.now(UTC).isoformat().replace("+00:00", "Z")
PHASE = {"name": "idle"}
TIMINGS: dict[str, float] = {}
EVIDENCE: dict[str, Any] = {}


def cl() -> httpx.Client:
    return httpx.Client(base_url=URL, timeout=120, headers=H)


def key(what: str) -> str:
    return f"{what}-{RUN}-{uuid.uuid4().hex[:6]}"


def post(c: httpx.Client, path: str, body: dict | None = None, ok: tuple[int, ...] = (200, 201, 202)) -> Any:
    r = c.post(path, json=body if body is not None else {})
    assert r.status_code in ok, f"POST {path} -> {r.status_code}: {r.text[:800]}"
    return r.json()


def get(c: httpx.Client, path: str, **params: Any) -> Any:
    r = c.get(path, params=params or None)
    assert r.status_code == 200, f"GET {path} -> {r.status_code}: {r.text[:500]}"
    return r.json()


@contextlib.contextmanager
def stage(name: str):
    PHASE["name"] = name
    t = time.monotonic()
    try:
        yield
    finally:
        TIMINGS[name] = round(time.monotonic() - t, 1)
        print(f"[stage] {name}: {TIMINGS[name]} s", flush=True)
        PHASE["name"] = "idle"


# --- resource sampling: both GPUs every 1 s, host RAM, container RAM every 15 s --------------------------------------
def _ram_mib() -> int:
    m = {ln.split(":")[0]: int(ln.split()[1]) for ln in Path("/proc/meminfo").read_text().splitlines()[:6]}
    return (m["MemTotal"] - m["MemAvailable"]) // 1024


def _sampler(stop: threading.Event) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    last_c = 0.0
    with (OUT / "gpu_samples.csv").open("w") as f, (OUT / "container_mem.jsonl").open("w") as fc:
        f.write("t,phase,gpu,mem_used_mib,util_pct,ram_used_mib\n")
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used,utilization.gpu",
                                      "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout
                ram, now = _ram_mib(), datetime.now(UTC).strftime("%H:%M:%S")
                for ln in out.strip().splitlines():
                    i, mem, util = (x.strip() for x in ln.split(","))
                    f.write(f"{now},{PHASE['name']},{i},{mem},{util},{ram}\n")
                f.flush()
                if time.monotonic() - last_c > 15:
                    last_c = time.monotonic()
                    st = subprocess.run(["podman", "stats", "--no-stream", "--format", "json"], capture_output=True,
                                        text=True, timeout=30).stdout
                    fc.write(json.dumps({"t": now, "phase": PHASE["name"], "stats": json.loads(st or "[]")}) + "\n")
                    fc.flush()
            except Exception as e:  # sampling must never break the run
                f.write(f"# sampler error {e!r}\n")
            stop.wait(max(0.0, 1.0 - (time.monotonic() - t0)))


def _summarise_samples() -> dict[str, Any]:
    rows = [ln.split(",") for ln in (OUT / "gpu_samples.csv").read_text().splitlines()[1:] if not ln.startswith("#")]
    out: dict[str, Any] = {"overall": {}, "by_phase": {}}
    for _, phase, gpu, mem, _u, ram in rows:
        for scope in (out["overall"], out["by_phase"].setdefault(phase, {})):
            scope[f"gpu{gpu}_peak_mib"] = max(scope.get(f"gpu{gpu}_peak_mib", 0), int(mem))
            scope["ram_peak_mib"] = max(scope.get("ram_peak_mib", 0), int(ram))
    out["samples"] = len(rows) // 2
    return out


@pytest.fixture(scope="module", autouse=True)
def evidence():
    with cl() as c:
        assert c.get("/api/health").json()["simulated"] is False, "must not run on the simulated engine"
        assert c.get(f"{P}/config").status_code == 200, f"project {PID} missing (this test never creates projects)"
    stop = threading.Event()
    th = threading.Thread(target=_sampler, args=(stop,), daemon=True)
    th.start()
    yield
    stop.set()
    th.join(timeout=10)
    passes = _passes_since(T0)
    (OUT / "passes.json").write_text(json.dumps(passes, indent=1))
    EVIDENCE["timings_s"] = TIMINGS
    EVIDENCE["resources"] = _summarise_samples()
    EVIDENCE["gpu0_passes"] = _pass_digest(passes, "gpu0")
    EVIDENCE["gpu1_passes"] = _pass_digest(passes, "gpu1")
    (OUT / "evidence.json").write_text(json.dumps(EVIDENCE, indent=1, default=str))


def _passes_since(t0: str) -> list[dict]:
    with cl() as c:
        ps = get(c, "/api/v2/passes", limit=500)["passes"]
    return sorted((p for p in ps if p["started_at"] >= t0 and p["task_ids"]), key=lambda p: p["started_at"])


def _secs(p: dict) -> float | None:
    if not p.get("ended_at"):
        return None
    a, b = (datetime.fromisoformat(p[k].replace("Z", "+00:00")) for k in ("started_at", "ended_at"))
    return round((b - a).total_seconds(), 1)


def _pass_digest(passes: list[dict], lane: str) -> dict[str, Any]:
    seq = [p for p in passes if p["lane"] == lane]
    res = [p["residency"] for p in seq]
    switches = sum(1 for a, b in zip(res, res[1:], strict=False) if a != b)
    meas = [p.get("measured") for p in seq]
    return {"passes": [{"residency": p["residency"], "tasks": len(p["task_ids"]), "seconds": _secs(p),
                        "started": p["started_at"], "measured": p.get("measured")} for p in seq],
            "residency_changes": switches,
            "measured_switched": sum(1 for m in meas if m and m.get("switched")) if any(meas) else "unavailable",
            "measured_model_loads": ("unavailable" if not any(m and m.get("model_loads") is not None for m in meas)
                                     else [m.get("model_loads") for m in meas])}


# --- waiting -----------------------------------------------------------------------------------------------------------
def active_tasks(c: httpx.Client) -> list[dict]:
    return [t for t in get(c, "/api/v2/tasks", project_id=PID, active=True)["tasks"] if t["created_at"] >= T0]


def check_failed(c: httpx.Client) -> None:
    for t in get(c, "/api/v2/tasks", project_id=PID)["tasks"]:
        if t["created_at"] >= T0 and t["state"] in ("failed",):
            raise AssertionError(f"task {t['stage']} {t['id']} failed: {json.dumps(t['error'])[:600]}")


def wait_for(c: httpx.Client, what: str, timeout: float, cond: Any, poll: float = 15.0) -> Any:
    end, blocked_since, idle_polls = time.monotonic() + timeout, None, 0
    while time.monotonic() < end:
        check_failed(c)
        act = active_tasks(c)
        if any(t["state"] == "blocked" for t in act):
            blocked_since = blocked_since or time.monotonic()
            if time.monotonic() - blocked_since > 180:
                raise AssertionError(f"{what}: blocked tasks {[(t['stage'], t['error']) for t in act]}")
        else:
            blocked_since = None
        val = cond()
        if val and not act:
            return val
        idle_polls = idle_polls + 1 if not act else 0
        if idle_polls >= 6 and not val:
            raise AssertionError(f"{what}: no active task for {idle_polls * poll:.0f} s but condition unmet")
        time.sleep(poll)
    raise AssertionError(f"timeout waiting for {what}")


# --- helpers over the API --------------------------------------------------------------------------------------------
def asset_by_name(c: httpx.Client, name_id: str) -> dict:
    hits = [a for a in get(c, f"{P}/assets", limit=500)["items"] if a["name_id"] == name_id]
    assert len(hits) == 1, f"{name_id}: {len(hits)} assets"
    return hits[0]


def art_bytes(c: httpx.Client, art_id: str) -> bytes:
    r = c.get(f"{P}/artifacts/{art_id}/content")
    assert r.status_code == 200, r.text[:200]
    return r.content


def run_items(c: httpx.Client, rid: str) -> list[dict]:
    run = get(c, f"{V2}/runs/{rid}")
    return [dict(i, job_id=j["id"]) for j in run["jobs"] for i in j["items"]]


def job_item(c: httpx.Client, jid: str) -> dict:
    return get(c, f"{V2}/jobs/{jid}")["items"][0]


def results(cand: dict) -> dict[str, dict]:
    return {r["rule_id"]: r for r in (cand.get("qa") or {}).get("results", [])}


def contact_sheet(src: bytes, cands: list[tuple[bytes, str]], path: Path, tile: int = 384) -> None:
    tiles = [(src, "SOURCE reference")] + cands
    sheet = Image.new("RGB", (tile * len(tiles), tile + 22), "white")
    d = ImageDraw.Draw(sheet)
    for n, (b, label) in enumerate(tiles):
        im = Image.open(io.BytesIO(b)).convert("RGBA")
        bg = Image.new("RGBA", im.size, (200, 200, 200, 255))
        im = Image.alpha_composite(bg, im).convert("RGB")
        im.thumbnail((tile, tile))
        sheet.paste(im, (n * tile, 22))
        d.text((n * tile + 4, 5), label, fill="black")
    sheet.save(path)


def variant_kick(c: httpx.Client, src: dict, method: str, rows: list[dict], candidates: int, tag: str,
                 intent: str = "related", **extra: Any) -> dict:
    """capabilities -> draft -> prepare-references -> (optional caller steps) happen in the callers; this makes the draft."""
    caps = get(c, f"{P}/assets/{src['asset_id']}/versions/{src['current_version_id']}/variant-capabilities")
    m = next(x for x in caps["methods"] if x["method"] == method)
    assert m["available"], f"{method} unavailable: {m['reason']} {m['message']}"
    EVIDENCE.setdefault(tag, {})["capabilities"] = m
    body = {"asset_id": src["asset_id"], "version_id": src["current_version_id"], "method": method, "intent": intent,
            "requested_variants": max(1, len(rows)) if rows else extra.pop("requested_variants", 1),
            "candidates_per_variant": candidates, "rows": rows, "idempotency_key": key(f"draft-{tag}"), **extra}
    return post(c, f"{P}/variant-drafts", body)


def prepare(c: httpx.Client, draft: dict, tag: str) -> dict:
    refs = post(c, f"{P}/variant-drafts/{draft['id']}:prepare-references")
    EVIDENCE[tag]["reference_warnings"] = refs.get("warnings")
    EVIDENCE[tag]["references"] = [{k: i[k] for k in ("view", "role", "sha256")} for i in refs["images"]]
    return refs


def enhance_wave(c: httpx.Client, out: dict, tag: str) -> str:
    bid = out["batch_id"]
    assert bid, "expected a Batch for a multi-row plan"
    with stage(f"{tag}.enhance"):
        plan = post(c, f"{V2}/batches/{bid}:plan", {})
        rid = post(c, f"{V2}/batches/{bid}:start", {"plan_id": plan["plan_id"], "plan_sha256": plan["plan_sha256"],
                                                    "idempotency_key": key("start")})["run_id"]
        wait_for(c, "prompt enhancement", 1800, lambda: all(i["current_prompt"] for i in run_items(c, rid)))
    EVIDENCE[tag]["prompts"] = {i["name"]: i["prompt"]["positive"] for i in run_items(c, rid)}
    return rid


def confirm_and_generate(c: httpx.Client, rid: str, tag: str, timeout: float) -> list[dict]:
    items = run_items(c, rid)
    with stage(f"{tag}.generate_qa"):
        res = post(c, f"{V2}/runs/{rid}:confirm-prompts", {"idempotency_key": key("confirm"), "items": [
            {"job_id": i["job_id"], "item_id": i["id"], "prompt_revision_id": i["current_prompt"],
             "expected_item_revision": i["revision"]} for i in items]})
        assert all(r["ok"] for r in res["results"]), res
        n = len(items)

        def done() -> bool:
            its = run_items(c, rid)
            return len(its) == n and all(i["candidate_set"] and i["candidate_set"]["candidates"] and all(
                x["qa"] for x in i["candidate_set"]["candidates"]) for i in its)
        wait_for(c, "candidate generation + QA", timeout, done)
    return run_items(c, rid)


def record_candidates(c: httpx.Client, items: list[dict], refs: dict, tag: str, problems: list[str],
                      conditioned: bool = True) -> None:
    prim = next(i for i in refs["images"] if i["role"] == "primary")
    src_png = art_bytes(c, prim["artifact_id"])
    for it in items:
        d = OUT / tag / it["name"].replace(" ", "_").replace("/", "_")
        d.mkdir(parents=True, exist_ok=True)
        (d / "source_primary_reference.png").write_bytes(src_png)
        cs, tiles, qa_out = it["candidate_set"], [], []
        gen = cs["generation"]
        if conditioned:
            ok = (gen.get("mode") == "image_edit" and gen.get("conditioning", {}).get("sha256") == prim["sha256"]
                  and not gen.get("simulated") and "qwen_image_edit_2511" in gen.get("models", []))
            if not ok:
                problems.append(f"{it['name']}: conditioning evidence missing/mismatch: {json.dumps(gen)[:400]}")
        shas = [x["sha256"] for x in cs["candidates"]]
        if len(set(shas)) != len(shas) or prim["sha256"] in shas:
            problems.append(f"{it['name']}: duplicate candidate or candidate == source reference")
        for x in cs["candidates"]:
            b = art_bytes(c, x["artifact_id"])
            (d / f"cand_{x['index']}.png").write_bytes(b)
            im = Image.open(io.BytesIO(b)).convert("RGB")
            if max(ImageStat.Stat(im).stddev) < 5:
                problems.append(f"{it['name']} cand {x['index']}: near-blank image")
            art = get(c, f"{P}/artifacts/{x['artifact_id']}")
            src = art.get("source") or {}
            r = results(x)
            tiles.append((b, f"#{x['index']} {x['qa']['status']}"))
            qa_out.append({"index": x["index"], "id": x["id"], "sha256": x["sha256"], "seed": x["seed"],
                           "size": [x["width"], x["height"]], "qa_status": x["qa"]["status"],
                           "failed_major": x["qa"]["policy"]["failed_major"],
                           "failed_minor": x["qa"]["policy"]["failed_minor"],
                           "unavailable": x["qa"]["policy"]["unavailable"],
                           "variant_checks": {k: {"result": v["result"], "reason": v["reason"]}
                                              for k, v in r.items() if k.startswith("variant_")},
                           "conditioning": {"artifact": (src.get("conditioning") or {}).get("artifact_id"),
                                            "sha256": (src.get("conditioning") or {}).get("sha256"),
                                            "workflow": (src.get("receipt") or {}).get("workflow"),
                                            "simulated": src.get("simulated")}})
        contact_sheet(src_png, tiles, d / "contact_sheet.png")
        (d / "qa_summary.json").write_text(json.dumps({"job": it["job_id"], "item": it["id"], "generation": gen,
                                                       "candidates": qa_out}, indent=1))


def choose(it: dict) -> tuple[dict, bool, str]:
    cands = it["candidate_set"]["candidates"]
    rec = [x for x in cands if x["qa"]["status"] == "recommended"]
    if rec:
        return rec[0], False, "recommended by QA"
    ok = [x for x in cands if all(results(x).get(k, {}).get("result") == "pass"
                                  for k in ("variant_change", "variant_resemblance"))]
    pool = ok or cands
    best = min(pool, key=lambda x: (len(x["qa"]["policy"]["failed_major"]), len(x["qa"]["policy"]["failed_minor"])))
    why = ("no candidate recommended; picked one passing variant_change+variant_resemblance" if ok else
           "no candidate recommended and none passes both variant checks; picked fewest failed rules")
    return best, True, f"{why} (status {best['qa']['status']}; failed {best['qa']['policy']['failed_major'] + best['qa']['policy']['failed_minor']})"


def approve_all(c: httpx.Client, rid: str, tag: str) -> dict[str, dict]:
    picks, body = {}, []
    for it in run_items(c, rid):
        cand, override, why = choose(it)
        picks[it["job_id"]] = {"name": it["name"], "candidate_index": cand["index"], "candidate_id": cand["id"],
                               "override": override, "reason": why}
        body.append({"job_id": it["job_id"], "item_id": it["id"], "expected_item_revision": it["revision"],
                     "candidate_set_id": it["candidate_set"]["id"], "candidate_id": cand["id"],
                     "image_sha256": cand["sha256"], "prompt_revision_id": it["candidate_set"]["prompt_revision_id"],
                     "qa_evaluation_id": cand["qa"]["id"], "override_qa": override,
                     "override_reason": why if override else None})
    res = post(c, f"{V2}/runs/{rid}:approve-candidates", {"idempotency_key": key("approve"), "items": body})
    assert all(r["ok"] for r in res["results"]), res
    EVIDENCE[tag]["approvals"] = picks
    return picks


def diversity(c: httpx.Client, plan_id: str, tag: str) -> None:
    with stage(f"{tag}.diversity"):
        post(c, f"{P}/variant-plans/{plan_id}:compare-selection", {"idempotency_key": key("cmp")})
        wait_for(c, "diversity", 900, lambda: get(c, f"{P}/variant-plans/{plan_id}/diversity")["status"] == "current",
                 poll=10)
    rep = get(c, f"{P}/variant-plans/{plan_id}/diversity")
    (OUT / tag).mkdir(parents=True, exist_ok=True)
    (OUT / tag / "diversity_report.json").write_text(json.dumps(rep, indent=1))
    EVIDENCE[tag]["diversity"] = {"coverage": rep["report"]["coverage"], "jobs": rep["jobs"],
                                  "pairs": [(p.get("result"), p.get("reason", "")[:160]) for p in rep["report"]["pairs"]],
                                  "exact_duplicates": rep["report"]["deterministic"]["exact_duplicates"]}


def build_wave(c: httpx.Client, rid: str, tag: str, timeout: float) -> list[dict]:
    items = run_items(c, rid)
    with stage(f"{tag}.build"):
        post(c, f"{V2}/runs/{rid}:build-approved", {"idempotency_key": key("build"), "items": [
            {"job_id": i["job_id"], "item_id": i["id"], "approval_id": i["approval"],
             "expected_item_revision": i["revision"]} for i in items]})
        wait_for(c, "builds", timeout, lambda: all(
            i["build"] and i["build"]["status"] in ("succeeded", "failed") for i in run_items(c, rid)), poll=20)
    return run_items(c, rid)


def publish_wave(c: httpx.Client, rid: str, tag: str, problems: list[str]) -> list[dict]:
    items = run_items(c, rid)
    valid = [i for i in items if i["build"] and i["build"]["result"] == "valid"]
    for i in items:
        if i not in valid:
            problems.append(f"{i['name']}: build not valid: status={i['build'] and i['build']['status']} "
                            f"result={i['build'] and i['build']['result']} "
                            f"failed={[x['id'] for x in (i['build'] or {}).get('validation', {}).get('checks', []) if not x['ok'] and x['id'] in (i['build'] or {}).get('validation', {}).get('required', [])]}")
    if not valid:
        return items
    with stage(f"{tag}.publish"):
        post(c, f"{V2}/runs/{rid}:accept-builds", {"idempotency_key": key("accept"), "items": [
            {"job_id": i["job_id"], "item_id": i["id"], "build_run_id": i["current_build"],
             "expected_item_revision": i["revision"]} for i in valid]})
        prev = get(c, f"{V2}/runs/{rid}/publish-preview")["items"]
        assert len(prev) == len(valid), prev
        post(c, f"{V2}/runs/{rid}:publish", {"idempotency_key": key("publish"), "items": [
            {"job_id": p["job_id"], "item_id": p["item_id"], "build_run_id": p["build_run_id"],
             "expected_item_revision": p["expected_item_revision"], "expected_current_version": p["current_version_id"]}
            for p in prev]})
        wait_for(c, "publication", 600, lambda: all(i["published"] for i in run_items(c, rid)
                                                    if i["build"] and i["build"]["result"] == "valid"))
    return run_items(c, rid)


def verify_published(c: httpx.Client, items: list[dict], src: dict, out: dict, tag: str, problems: list[str],
                     role_prefix: str | None = None, method: str | None = None) -> None:
    rows = []
    for i in items:
        if not (i["build"] and i["build"]["result"] == "valid"):
            continue
        pub = i["published"]
        if not pub:
            problems.append(f"{i['name']}: not published")
            continue
        a = get(c, f"{P}/assets/{pub['asset_id']}")
        m, v = a["manifest"], a["shown_version"]
        der = v.get("derivation") or {}
        good = (m["origin"] == "derived" and m["family_id"] == out["family_id"] and len(m["versions"]) == 1
                and v["display_version"] == 1 and der.get("source", {}).get("asset_id") == src["asset_id"]
                and der.get("source", {}).get("version_id") == src["current_version_id"])
        if method:
            good = good and der.get("method") == method
        files = sorted(f["role"] for f in a["files"])
        if role_prefix and not [f for f in files if f.startswith(role_prefix)]:
            good = False
            problems.append(f"{i['name']}: file roles {files} have no {role_prefix}<size>")
        if not good:
            problems.append(f"{i['name']}: derived-asset checks failed: origin={m['origin']} family={m['family_id']} "
                            f"vers={len(m['versions'])} derivation={json.dumps(der)[:300]}")
        rows.append({"job": i["name"], "asset_id": pub["asset_id"], "name_id": m["name_id"], "origin": m["origin"],
                     "family_id": m["family_id"], "display_version": v["display_version"], "files": files,
                     "derivation_source": der.get("source"), "method": der.get("method")})
    EVIDENCE[tag]["published"] = rows


def build_evidence(c: httpx.Client, items: list[dict], tag: str) -> None:
    rows = []
    for i in items:
        b = i["build"]
        d = OUT / tag / i["name"].replace(" ", "_").replace("/", "_")
        d.mkdir(parents=True, exist_ok=True)
        if b and b["artifacts"].get("preview"):
            (d / "final_preview.png").write_bytes(art_bytes(c, b["artifacts"]["preview"]))
        rows.append({"job": i["name"], "status": b and b["status"], "result": b and b["result"],
                     "seconds": b and round((datetime.fromisoformat(b["updated_at"].replace("Z", "+00:00")) -
                                             datetime.fromisoformat(b["created_at"].replace("Z", "+00:00"))
                                             ).total_seconds(), 1),
                     "checks": {x["id"]: x.get("detail", x["ok"]) for x in (b or {}).get("validation", {}).get("checks", [])
                                if not x["ok"] or x["id"] in ("non_empty", "triangle_budget", "final_height")}})
    EVIDENCE[tag]["builds"] = rows


def generative_scenario(tag: str, name_id: str, method: str, rows: list[dict], candidates: int, timeout_gen: float,
                        planner: bool = False, request: str = "") -> None:
    problems: list[str] = []
    EVIDENCE[tag] = {"problems": problems}
    with cl() as c:
        src = asset_by_name(c, name_id)
        before = get(c, f"{P}/assets/{src['asset_id']}")["manifest"]
        d = variant_kick(c, src, method, [] if planner else rows, candidates, tag,
                         requested_variants=len(rows) if planner else 1)
        refs = prepare(c, d, tag)
        if planner:
            with stage(f"{tag}.plan_vlm"):
                post(c, f"{P}/variant-drafts/{d['id']}:analyze-source", {"idempotency_key": key("an")})
                wait_for(c, "analyze-source", 900, lambda: get(c, f"{P}/variant-drafts/{d['id']}")["tasks"]["analyze"][
                    "state"] == "succeeded", poll=5)
                post(c, f"{P}/variant-drafts/{d['id']}:suggest-plan", {"count": len(rows), "request": request,
                                                                          "idempotency_key": key("sg")})
                cur = wait_for(c, "suggest-plan", 900, lambda: (lambda x: x if x["tasks"]["suggest"]["state"] ==
                                                                "succeeded" else None)(get(c, f"{P}/variant-drafts/{d['id']}")),
                               poll=5)
            EVIDENCE[tag]["analysis"] = get(c, f"{P}/variant-drafts/{d['id']}/analysis").get("observations")
            EVIDENCE[tag]["suggestion"] = cur["suggestion"]["rows"]
            post(c, f"{P}/variant-drafts/{d['id']}:apply-suggestion", {"expected_revision": cur["revision"],
                                                                        "mode": "replace_empty"})
        d = get(c, f"{P}/variant-drafts/{d['id']}")
        assert len(d["rows"]) == len(rows), d["rows"]
        EVIDENCE[tag]["rows"] = [{"label": r["label"], "change_request": r["change_request"],
                                  "candidates": r["candidate_count"]} for r in d["rows"]]
        out = post(c, f"{P}/variant-drafts/{d['id']}:create-jobs", {"expected_revision": d["revision"],
                                                                      "idempotency_key": key("jobs")})
        EVIDENCE[tag]["summary"] = out["summary"]
        assert len(out["job_ids"]) == len(rows) and out["batch_id"], out
        rid = enhance_wave(c, out, tag)
        items = confirm_and_generate(c, rid, tag, timeout_gen)
        record_candidates(c, items, refs, tag, problems)
        approve_all(c, rid, tag)
        diversity(c, out["plan_id"], tag)
        items = build_wave(c, rid, tag, 7200)
        build_evidence(c, items, tag)
        items = publish_wave(c, rid, tag, problems)
        verify_published(c, items, src, out, tag, problems, role_prefix="icon_" if method == "image_edit" else None)
        after = get(c, f"{P}/assets/{src['asset_id']}")["manifest"]
        if after["versions"] != before["versions"] or after["current_version_id"] != before["current_version_id"]:
            problems.append("SOURCE versions changed")
        EVIDENCE[tag]["source_versions_unchanged"] = after["versions"] == before["versions"]
    EVIDENCE[tag]["passes"] = _pass_digest(_passes_since(T0), "gpu0")["passes"][-6:]
    assert not problems, "; ".join(problems)


PINE_ROWS = [{"label": x} for x in ("compact", "narrow", "broad", "sparse", "tall", "asymmetric")]


def test_1_pine_six_structural_variants() -> None:
    generative_scenario("pine", "pine_tree_medium_polygon_count", "image_edit_reconstruct", PINE_ROWS, 4, 6 * 3600,
                        planner=True, request="six related pine variants: compact, narrow, broad, sparse, tall, "
                                              "asymmetric")


def test_2_crate_two_manual_rows() -> None:
    rows = [{"label": "Tall narrow crate", "change_request": "a taller, narrower crate"},
            {"label": "Crate with lid and rope handles", "change_request": "crate with a fitted lid and rope handles"}]
    generative_scenario("crate", "wooden_supply_crate", "image_edit_reconstruct", rows, 2, 3 * 3600)


def test_3_t2i_unaffected_after_edit() -> None:
    problems: list[str] = []
    EVIDENCE["t2i"] = {"problems": problems}
    with cl() as c:
        before = {p["id"] for p in _passes_since(T0)}
        out = post(c, f"{V2}/jobs", {"title": f"T2I check {RUN}", "category_id": "props", "candidate_count": 2,
                                     "items": [{"name": f"Round oak barrel {RUN}",
                                                "brief": "round oak barrel with iron hoops"}],
                                     "idempotency_key": key("t2i"), "run": True})
        jid = out["job"]["id"]
        with stage("t2i.enhance"):
            wait_for(c, "t2i enhance", 900, lambda: job_item(c, jid)["current_prompt"], poll=5)
        it = job_item(c, jid)
        with stage("t2i.generate_qa"):
            post(c, f"{V2}/jobs/{jid}:confirm-and-generate", {"idempotency_key": key("conf"), "items": [
                {"item_id": it["id"], "prompt_revision_id": it["current_prompt"], "expected_item_revision": it["revision"]}]})
            wait_for(c, "t2i candidates", 1800, lambda: (lambda s: s and len(s["candidates"]) == 2 and all(
                x["qa"] for x in s["candidates"]))(job_item(c, jid)["candidate_set"]), poll=10)
        cs = job_item(c, jid)["candidate_set"]
        gen = cs["generation"]
        d = OUT / "t2i"
        d.mkdir(parents=True, exist_ok=True)
        rows = []
        for x in cs["candidates"]:
            b = art_bytes(c, x["artifact_id"])
            (d / f"cand_{x['index']}.png").write_bytes(b)
            sd = max(ImageStat.Stat(Image.open(io.BytesIO(b)).convert("RGB")).stddev)
            rows.append({"index": x["index"], "sha256": x["sha256"], "qa": x["qa"]["status"], "stddev": round(sd, 1)})
            if sd < 5:
                problems.append(f"t2i cand {x['index']} near-blank")
        EVIDENCE["t2i"].update({"job": jid, "workflow": gen["workflow"], "params": gen["params"],
                                "models": gen["models"], "candidates": rows})
        if gen["workflow"] != "comfyui.qwen_t2i" or gen.get("simulated"):
            problems.append(f"unexpected workflow {gen['workflow']}")
    new = [p for p in _passes_since(T0) if p["id"] not in before and p["lane"] == "gpu0"]
    EVIDENCE["t2i"]["gpu0_passes_in_scenario"] = [(p["residency"], p.get("measured")) for p in new]
    assert not problems, "; ".join(problems)


def test_4_icon_two_manual_rows() -> None:
    rows = [{"label": "Blue mana potion", "change_request": "blue mana potion, same bottle shape"},
            {"label": "Corked sealed potion", "change_request": "potion with a cork and a wax seal"}]
    generative_scenario("icon", "healing_potion", "image_edit", rows, 2, 3 * 3600)


def _comfy_history_ids() -> set[str]:
    r = httpx.get(f"{COMFY}/history?max_items=2000", timeout=30)
    return set(r.json())


def test_5_direct_transforms_touch_no_gpu() -> None:
    problems: list[str] = []
    EVIDENCE["direct"] = {"problems": problems}
    hist0, log_t = _comfy_history_ids(), datetime.now(UTC).isoformat()
    queue0 = httpx.get(f"{COMFY}/queue", timeout=30).json()
    gpu_passes0 = len([p for p in _passes_since(T0) if p["lane"] in ("gpu0", "gpu1")])
    with cl() as c:
        src = asset_by_name(c, "treasure_chest")
        before = get(c, f"{P}/assets/{src['asset_id']}")["manifest"]
        rows = [{"label": "Chest 1.2 m", "glb_transform": {"op": "target_height", "height_m": 1.2,
                                                           "anchor": "bottom_center", "units_confirmed": True}},
                {"label": "Chest half size", "glb_transform": {"op": "uniform_scale", "factor": 0.5}}]
        d = variant_kick(c, src, "direct_transform", rows, 1, "direct")
        out = post(c, f"{P}/variant-drafts/{d['id']}:create-jobs", {"expected_revision": d["revision"],
                                                                      "idempotency_key": key("jobs")})
        EVIDENCE["direct"]["summary"] = out["summary"]
        assert out["summary"]["image_edits"] == 0
        with stage("direct.transform_publish"):
            for jid in out["job_ids"]:
                it = job_item(c, jid)
                post(c, f"{V2}/jobs/{jid}:run-transform", {"idempotency_key": key("rt"), "items": [
                    {"item_id": it["id"], "expected_item_revision": it["revision"]}]})
            wait_for(c, "transforms", 300, lambda: all(job_item(c, j)["build"] and job_item(c, j)["build"]["status"]
                                                       in ("succeeded", "failed") for j in out["job_ids"]), poll=3)
            items = []
            for jid in out["job_ids"]:
                it = job_item(c, jid)
                b = it["build"]
                bad = [x["id"] for x in b["validation"]["checks"] if not x["ok"]]
                if b["result"] != "valid" or bad:
                    problems.append(f"{it['name']}: result={b['result']} failed checks {bad}")
                EVIDENCE["direct"].setdefault("checks", {})[it["name"]] = {x["id"]: x.get("detail", x["ok"])
                                                                            for x in b["validation"]["checks"]}
                (OUT / "direct").mkdir(parents=True, exist_ok=True)
                if b["artifacts"].get("preview"):
                    (OUT / "direct" / f"{it['name'].replace(' ', '_')}_preview.png").write_bytes(
                        art_bytes(c, b["artifacts"]["preview"]))
                post(c, f"{V2}/jobs/{jid}:accept-builds", {"idempotency_key": key("acc"), "items": [
                    {"item_id": it["id"], "build_run_id": it["current_build"], "expected_item_revision": it["revision"]}]})
                prev = get(c, f"{V2}/jobs/{jid}/publish-preview")["items"]
                post(c, f"{V2}/jobs/{jid}:publish", {"idempotency_key": key("pub"), "items": [
                    {"item_id": p["item_id"], "build_run_id": p["build_run_id"],
                     "expected_item_revision": p["expected_item_revision"],
                     "expected_current_version": p["current_version_id"]} for p in prev]})
            wait_for(c, "direct publish", 300, lambda: all(job_item(c, j)["published"] for j in out["job_ids"]), poll=3)
            items = [dict(job_item(c, j), job_id=j) for j in out["job_ids"]]
        verify_published(c, items, src, out, "direct", problems, method="direct_transform")
        tasks = [t for j in out["job_ids"] for t in get(c, "/api/v2/tasks", project_id=PID, job_id=j)["tasks"]]
        lanes = sorted({(t["stage"], t["lane"]) for t in tasks})
        EVIDENCE["direct"]["task_lanes"] = lanes
        if any(lane in ("gpu0", "gpu1") for _, lane in lanes):
            problems.append(f"GPU lane used by direct Jobs: {lanes}")
        after = get(c, f"{P}/assets/{src['asset_id']}")["manifest"]
        if after["versions"] != before["versions"]:
            problems.append("source versions changed")
    hist1, queue1 = _comfy_history_ids(), httpx.get(f"{COMFY}/queue", timeout=30).json()
    gpu_passes1 = len([p for p in _passes_since(T0) if p["lane"] in ("gpu0", "gpu1")])
    w3d = subprocess.run(["podman", "logs", "--since", log_t, "assetstudio_worker3d_1"], capture_output=True, text=True)
    lines = [ln for ln in (w3d.stdout + w3d.stderr).splitlines() if "GET /health" not in ln and ln.strip()]
    EVIDENCE["direct"]["engine_activity"] = {
        "comfy_history_new_prompts": len(hist1 - hist0), "comfy_queue_before": [len(queue0["queue_running"]), len(
            queue0["queue_pending"])], "comfy_queue_after": [len(queue1["queue_running"]), len(queue1["queue_pending"])],
        "gpu_passes_before": gpu_passes0, "gpu_passes_after": gpu_passes1, "worker3d_log_lines_during": lines[:20]}
    if hist1 != hist0 or gpu_passes1 != gpu_passes0:
        problems.append("ComfyUI history or GPU passes changed during direct transforms")
    if lines:
        problems.append(f"worker3d logged {len(lines)} lines during the transforms (first: {lines[0][:160]})")
    assert not problems, "; ".join(problems)

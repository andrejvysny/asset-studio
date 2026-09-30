"""Model closure verifier (R05), GPU handoff acks (R02), ComfyUI adapter bindings/cancel (E1 contract)."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from assetstudio_server.adapters.aux import AuxClient
from assetstudio_server.adapters.base import AckError, LoraUse, T2IRequest
from assetstudio_server.adapters.comfyui import ComfyEngine, Workflow
from assetstudio_server.gpu import GpuLane, LaneWorker, OwnershipUnknown
from assetstudio_server.models import verify_model

ROOT = Path(__file__).resolve().parents[2]


def spec(files: dict, gated: bool = False) -> dict:
    return {"repo": "r", "revision": "x", "local_dir": "m", "files": files, "gated": gated}


def test_model_closure(tmp_path: Path) -> None:
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "README.md").write_text("hi")
    assert verify_model("k", spec({}), tmp_path).status == "invalid_lock"
    st = verify_model("k", spec({"model.safetensors": {"size": 3, "sha256": None}}), tmp_path)
    assert st.status == "missing" and not st.ready  # README present is not enough
    (tmp_path / "m" / "model.safetensors").write_bytes(b"abc")
    import hashlib
    good = hashlib.sha256(b"abc").hexdigest()
    assert verify_model("k", spec({"model.safetensors": {"size": 3, "sha256": good}}), tmp_path, full=True).ready
    bad = verify_model("k", spec({"model.safetensors": {"size": 3, "sha256": "0" * 64}}), tmp_path, full=True)
    assert bad.status == "corrupt"
    assert verify_model("k", spec({"model.safetensors": {"size": 4, "sha256": good}}), tmp_path).status == "corrupt"
    pend = verify_model("k", spec({"w.bin": {"size": 1, "sha256": None}}, gated=True), tmp_path)
    assert pend.status == "pending_access"
    (tmp_path / "outside").write_bytes(b"abc")
    (tmp_path / "m" / "link").symlink_to(tmp_path.parent)
    esc = verify_model("k", spec({"link/outside": {"size": 3, "sha256": good}}), tmp_path)
    assert esc.status == "corrupt" and "outside" in esc.problems[0]


def test_repo_lock_pins_dinov3() -> None:
    """Gated access was granted and every file hash pinned (2026-09-29): absent weights read as missing, never
    as pending access, and only a verified download is ready."""
    from assetstudio_server.models import load_lock, verify_all
    files = load_lock(ROOT / "config")["models"]["dinov3_vitl16"]["files"]
    assert files and all(f.get("sha256") for f in files.values())
    st = verify_all(ROOT / "config", ROOT / "models")["dinov3_vitl16"]
    assert st.status in ("missing", "incomplete", "ok") and (st.status == "ok") == st.ready


def aux_with(handler) -> AuxClient:
    return AuxClient("http://aux", httpx.Client(transport=httpx.MockTransport(handler), base_url="http://aux"))


def _ok_release(r: httpx.Request) -> httpx.Response:
    body = json.loads(r.content)
    return httpx.Response(200, json={"loaded": False, "owner_token": body["owner_token"], "epoch": body["epoch"],
                                     "active": 0, "admitting": False, "session_id": "s1"})


def _worker(unload, lease=None) -> LaneWorker:
    return LaneWorker(lease or (lambda e: {"epoch": e, "admitting": True, "session_id": "w", "active": 0}), unload)


def _lane(workers: dict[str, LaneWorker]) -> GpuLane:
    counter = iter(range(1, 1000))
    return GpuLane("gpu1", workers, lambda: next(counter))


@pytest.mark.parametrize("responder", [
    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("t")),
    lambda r: (_ for _ in ()).throw(httpx.ConnectError("c")),
    lambda r: (_ for _ in ()).throw(httpx.RemoteProtocolError("reset")),
    lambda r: httpx.Response(500, json={"loaded": False}),
    lambda r: httpx.Response(409, json={"detail": "GPU work still active"}),
    lambda r: httpx.Response(200, text="not json"),
    lambda r: httpx.Response(200, json={**json.loads(_ok_release(r).content), "loaded": True}),
    lambda r: httpx.Response(200, json={**json.loads(_ok_release(r).content), "owner_token": "someone-else"}),
    lambda r: httpx.Response(200, json={**json.loads(_ok_release(r).content), "active": 1}),
    lambda r: httpx.Response(200, json={**json.loads(_ok_release(r).content), "epoch": 0}),
    lambda r: httpx.Response(200, json={**json.loads(_ok_release(r).content), "admitting": True}),
])
def test_unload_without_explicit_ack_blocks_handoff(responder) -> None:
    """RI11: timeout, reset, malformed, loaded=true, wrong token/epoch, active work -> ownership stays unknown."""
    aux = aux_with(responder)
    lane = _lane({"aux": _worker(aux.unload), "worker3d": _worker(lambda t, e: json.loads(
        _ok_release(httpx.Request("POST", "/", json={"owner_token": t, "epoch": e})).content))})
    with pytest.raises(OwnershipUnknown):
        lane.acquire("worker3d")
    assert lane.state == "unknown" and lane.owner is None


def test_explicit_ack_grants_with_increasing_epochs() -> None:
    aux = aux_with(_ok_release)
    w3d = LaneWorker(lambda e: {"epoch": e, "admitting": True, "session_id": "w3"},
                     lambda t, e: {"loaded": False, "owner_token": t, "epoch": e, "active": 0, "admitting": False})
    lane = _lane({"aux": LaneWorker(lambda e: {"epoch": e, "admitting": True, "session_id": "a"}, aux.unload),
                  "worker3d": w3d})
    e1 = lane.acquire("worker3d")
    assert lane.owner == "worker3d" and lane.state == "owned" and lane.acquire("worker3d") == e1
    e2 = lane.acquire("aux")  # worker3d must now ack for the new epoch
    assert lane.owner == "aux" and e2 > e1 and lane.sessions == {"worker3d": "w3", "aux": "a"}
    with pytest.raises(OwnershipUnknown):
        lane.epoch_for("worker3d")


def test_rejected_lease_keeps_ownership_unknown() -> None:
    def refuse(e: int) -> dict:
        raise AckError("stale")
    lane = _lane({"worker3d": LaneWorker(refuse, lambda t, e: {})})
    with pytest.raises(OwnershipUnknown):
        lane.acquire("worker3d")
    assert lane.state == "unknown"


def test_worker_lease_drain_waits_for_export_and_fences_stale_epochs() -> None:
    """RI10: unload during export cannot acknowledge release early; nothing is admitted during/after drain."""
    import sys
    import threading
    import time

    sys.path.insert(0, str(ROOT / "services" / "worker_common"))
    from lease import Lease, StaleLease

    lease = Lease()
    lease.grant(3)
    lease.enter(3)  # an export (or sampling, transfer) in flight
    released: list[float] = []
    t0 = time.monotonic()
    th = threading.Thread(target=lambda: released.append(lease.drain(4, timeout=5.0) and time.monotonic() - t0))
    th.start()
    time.sleep(0.2)
    with pytest.raises(StaleLease):
        lease.enter(3)  # admission stopped the moment drain began
    with pytest.raises(StaleLease):
        lease.enter(4)
    assert not released  # still waiting for the export
    lease.leave()
    th.join(2)
    assert released and released[0] >= 0.2
    with pytest.raises(StaleLease):
        lease.grant(2)  # a Studio holding an older epoch can never regain the device
    assert lease.grant(5)["admitting"] is True


def req(**kw) -> T2IRequest:
    base = dict(prompt_id="11111111-1111-5111-8111-111111111111", positive="p", negative="n", seed=7, width=64,
                height=64, steps=4, cfg=1.0, filename_prefix="x")
    base.update(kw)
    return T2IRequest(**base)


def test_graph_bindings_and_optional_lora_removal() -> None:
    wf = Workflow(ROOT / "comfyui" / "workflows", "image.t2i.qwen.bindings.yaml")
    g = wf.build(req())
    assert "4" not in g and "5" not in g and g["6"]["inputs"]["model"] == ["1", 0]
    assert g["10"]["inputs"]["seed"] == 7 and g["7"]["inputs"]["text"] == "p"
    g2 = wf.build(req(speed_lora=LoraUse("fast.safetensors", 1.0), style_lora=LoraUse("s.safetensors", 0.0)))
    assert g2["4"]["inputs"]["strength_model"] == 0.0 and g2["6"]["inputs"]["model"] == ["5", 0]
    assert g2["5"]["inputs"]["model"] == ["4", 0]


def test_comfy_submit_status_and_targeted_cancel() -> None:
    seen: list[tuple[str, str, bytes]] = []
    history: dict = {}

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append((r.method, r.url.path, r.content))
        if r.url.path == "/prompt":
            body = json.loads(r.content)
            return httpx.Response(200, json={"prompt_id": body["prompt_id"], "number": 1})
        if r.url.path.startswith("/history/"):
            return httpx.Response(200, json=history)
        if r.url.path == "/queue" and r.method == "GET":
            return httpx.Response(200, json={"queue_running": [[1, "abc"]], "queue_pending": []})
        return httpx.Response(200, json={})

    eng = ComfyEngine("http://c", Workflow(ROOT / "comfyui" / "workflows", "image.t2i.qwen.bindings.yaml"),
                      httpx.Client(transport=httpx.MockTransport(handler), base_url="http://c"))
    r = req()
    assert eng.submit(r) == r.prompt_id
    assert json.loads(seen[0][2])["prompt_id"] == r.prompt_id
    assert eng.status(r.prompt_id).state == "unknown"
    assert eng.status("abc").state == "running"
    history["abc"] = {"status": {"status_str": "error", "completed": False,
                                 "messages": [["execution_error", {"exception_message": "OOM"}]]}}
    st = eng.status("abc")
    assert st.state == "failed" and st.error == "OOM"
    seen.clear()
    eng.cancel("abc")
    assert [(m, p) for m, p, _ in seen] == [("POST", "/queue"), ("POST", "/interrupt")]
    assert json.loads(seen[1][2]) == {"prompt_id": "abc"}  # never a global interrupt

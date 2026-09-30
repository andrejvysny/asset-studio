"""EngineExecutor, recovery barrier and model receipts against the fake engines (no GPU, no network)."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from assetstudio_core.canonical import sha256_json
from assetstudio_core.ids import new_id
from assetstudio_node import models as models_mod
from assetstudio_node import receipts as receipts_mod
from assetstudio_node.barrier import recover_slots
from assetstudio_node.config import RunnerConfig
from assetstudio_node.engine_executor import EngineExecutor, Engines
from assetstudio_node.engines.fake import FakeAux, FakeEngine, FakeWorker3d
from assetstudio_node.executor import (
    ExecutionBlocked,
    ExecutionCancelled,
    ExecutionFailed,
    FakeExecutor,
)
from assetstudio_node.models import HashCache, load_lock
from assetstudio_node.receipts import lock_identity, model_receipts, session_receipts
from assetstudio_node.state import RunnerState
from assetstudio_protocol import calls
from assetstudio_protocol.engine import AckError, T2IRequest
from assetstudio_protocol.execution import (
    OPERATION_CAPABILITY,
    OPERATION_ENGINE,
    OPERATION_VERSIONS,
    InputRef,
    Offer,
    Policy,
    Requirements,
    compute_input_digest,
)
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
RID, SID = new_id("rnr"), new_id("rse")
T2I = {"prompt_id": "p1", "positive": "a cat", "negative": "", "seed": 7, "width": 64, "height": 64, "steps": 4,
       "cfg": 1.0, "filename_prefix": "x"}


def png(color: int = 120) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (16, 16), (color, 30, 30)).save(out, "PNG")
    return out.getvalue()


def make_offer(op: str, params: dict[str, Any], inputs: list[tuple[bytes, str, str]] | None = None) -> Offer:
    refs = [InputRef(sha256=hashlib.sha256(b).hexdigest(), size=len(b), role=r, label=label, mime="image/png")
            for b, r, label in inputs or []]
    req = Requirements(capability=OPERATION_CAPABILITY[op], engine=OPERATION_ENGINE[op])
    slot = "gpu0" if op.startswith("image.") else "gpu1"
    return Offer(schema="assetstudio.execution.v1", attempt_id=new_id("atp"), task_id="t", call_key="j/1",
                 generation=1, runner_id=RID, session_id=SID, slot_id=slot, operation=op,
                 operation_version=OPERATION_VERSIONS[op],
                 input_digest=compute_input_digest(op, OPERATION_VERSIONS[op], refs, params, req, Policy()),
                 inputs=refs, params=params, requirements=req, offer_expires_at="2026-01-01T00:00:00Z")


def config(tmp_path: Path) -> RunnerConfig:
    return RunnerConfig.model_validate({
        "studio_url": "http://studio.test", "name": "r1", "state_dir": str(tmp_path / "state"), "simulated": True,
        "slots": [{"slot_id": "gpu0", "capability": "image", "devices": ["GPU-a"], "engines": ["comfyui"]},
                  {"slot_id": "gpu1", "capability": "aux3d", "devices": ["GPU-b"], "engines": ["aux", "worker3d"]}]})


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.config = config(tmp_path)
        self.state = RunnerState(self.config.state_dir)
        self.comfy, self.aux, self.w3d = FakeEngine(), FakeAux(), FakeWorker3d()
        self.ex = EngineExecutor(self.config, self.state, Engines(self.comfy, self.aux, self.w3d),
                                 sleep=lambda _s: None)
        self.n = 0

    def run(self, offer: Offer, blobs: list[bytes] | None = None, cancel: Any = lambda: False) -> tuple[Any, Any]:
        self.n += 1
        inputs = {}
        for ref, data in zip(offer.inputs, blobs or [], strict=True):
            path = self.tmp / f"in_{ref.sha256}"
            path.write_bytes(data)
            inputs[ref.sha256] = path
        return self.ex.execute(offer, inputs, self.tmp / f"out{self.n}", cancel)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def by_name(outputs: list[tuple[str, Path, str]]) -> dict[str, tuple[bytes, str]]:
    return {n: (p.read_bytes(), m) for n, p, m in outputs}


# -- image ----------------------------------------------------------------------------------------------------------


def test_t2i_outputs_and_meta(rig: Rig) -> None:
    outputs, meta = rig.run(make_offer("image.t2i", T2I))
    files = by_name(outputs)
    assert list(files) == ["image.png"] and files["image.png"][1] == "image/png"
    assert files["image.png"][0].startswith(b"\x89PNG")
    assert meta["prompt_id"] == "p1" and meta["simulated"] is True and meta["engine"]["engine"] == "fake"
    assert meta["workflow"] == {"id": "fake.t2i", "version": 0, "graph_sha256": None}
    assert rig.comfy.calls[0] == ("submit", "p1")


def test_t2i_reconciles_by_prompt_id_instead_of_resubmitting(rig: Rig) -> None:
    rig.comfy.submit(T2IRequest(**T2I))  # the engine already knows this prompt (e.g. lost submit response)
    rig.run(make_offer("image.t2i", T2I))
    assert [c for c in rig.comfy.calls if c[0] == "submit"] == [("submit", "p1")]


def test_t2i_cancel_during_polling_cancels_engine_prompt(rig: Rig) -> None:
    rig.comfy.steps_to_finish = 99
    polls = {"n": 0}

    def cancel() -> bool:
        polls["n"] += 1
        return polls["n"] > 2

    with pytest.raises(ExecutionCancelled):
        rig.run(make_offer("image.t2i", T2I), cancel=cancel)
    assert ("cancel", "p1") in rig.comfy.calls


def test_t2i_failed_prompt_is_internal_failure(rig: Rig) -> None:
    rig.comfy.fail_prompts.add("p1")
    with pytest.raises(ExecutionFailed) as e:
        rig.run(make_offer("image.t2i", T2I))
    assert e.value.code == "internal" and "simulated failure" in str(e.value)


def test_edit_uses_input_and_reports_receipt(rig: Rig) -> None:
    src = png()
    sha = hashlib.sha256(src).hexdigest()
    params = {"prompt_id": "e1", "source_sha256": sha, "prepared_input_sha256": sha, "positive": "p",
              "negative": "n", "seed": 3, "steps": 4, "cfg": 1.0, "filename_prefix": "x"}
    outputs, meta = rig.run(make_offer("image.edit", params, [(src, "image", "")]), [src])
    assert by_name(outputs)["image.png"][0].startswith(b"\x89PNG")
    assert meta["receipt"]["workflow"] == "fake.image_edit" and meta["workflow"]["id"] == "fake.image_edit"
    assert rig.comfy.calls[0] == ("submit_edit", "e1")
    rig.run(make_offer("image.edit", params, [(src, "image", "")]), [src])  # known prompt: reconciled, not resubmitted
    assert [c for c in rig.comfy.calls if c[0] == "submit_edit"] == [("submit_edit", "e1")]


def test_edit_rejection_and_bad_inputs_are_input_invalid(rig: Rig) -> None:
    src = png()
    bad = {"prompt_id": "e2", "source_sha256": "b" * 64, "prepared_input_sha256": "b" * 64, "positive": "p",
           "negative": "n", "seed": 3, "steps": 4, "cfg": 1.0, "filename_prefix": "x"}
    with pytest.raises(ExecutionFailed) as e:  # EngineRejected: prepared input hash mismatch
        rig.run(make_offer("image.edit", bad, [(src, "image", "")]), [src])
    assert e.value.code == "input_invalid"
    with pytest.raises(ExecutionFailed) as e:  # no input at all
        rig.run(make_offer("image.edit", bad))
    assert e.value.code == "input_invalid"
    with pytest.raises(ExecutionFailed) as e:  # malformed params
        rig.run(make_offer("image.t2i", {"prompt_id": "x"}))
    assert e.value.code == "input_invalid"
    offer = make_offer("image.edit", bad, [(src, "image", "")])
    with pytest.raises(ExecutionFailed) as e:  # input file not provided
        rig.ex.execute(offer, {}, rig.tmp / "o", lambda: False)
    assert e.value.code == "input_invalid"


def test_engine_unavailable_is_blocked_not_failed(rig: Rig) -> None:
    rig.comfy.down = True
    with pytest.raises(ExecutionBlocked) as e:
        rig.run(make_offer("image.t2i", T2I))
    assert e.value.code == "node_unavailable"
    rig.ex.engines.comfy = None
    with pytest.raises(ExecutionBlocked):
        rig.run(make_offer("image.t2i", T2I))


# -- aux ------------------------------------------------------------------------------------------------------------

AUX_CASES = {
    "aux.enhance": ({"execution_id": "x1", "brief": "a mug", "kind": "prop", "constraints": "c", "style_guide": "s"},
                    [(b"ref", "reference", "note")]),
    "aux.compare": ({"execution_id": "x2", "questions": [["change_1", "changed?"]], "context": "c"},
                    [(b"aaa", "source", "A"), (b"bbb", "candidate", "B")]),
    "aux.qa": ({"execution_id": "x3", "questions": [["q1", "ok?"]], "context": "c"}, [(png(), "image", "")]),
    "aux.cutout": ({"execution_id": "x4"}, [(png(), "image", "")]),
    "aux.analyze_source": ({"execution_id": "x5", "kind": "prop"}, [(png(), "front", "")]),
    "aux.suggest_variants": ({"execution_id": "x6", "request": "r", "count": 2, "intent": "i", "preserve": "p",
                              "kind": "prop"}, [(png(), "front", "")]),
}


@pytest.mark.parametrize("op", sorted(AUX_CASES))
def test_aux_operations_round_trip_results(rig: Rig, op: str) -> None:
    params, inputs = AUX_CASES[op]
    outputs, meta = rig.run(make_offer(op, params, inputs), [b for b, _, _ in inputs])
    files = by_name(outputs)
    assert list(files)[0] == "result.json" and files["result.json"][1] == "application/json"
    result = calls.decode_result(files["result.json"][0], {n: d for n, (d, _) in files.items()})
    assert meta["simulated"] is True and rig.aux.calls[-1] == op.removeprefix("aux.")
    if op == "aux.cutout":
        assert list(files) == ["result.json", "mask_png"] and result["mask_png"].startswith(b"\x89PNG")
        assert files["mask_png"][1] == "application/octet-stream"
    else:
        assert list(files) == ["result.json"]
    if op == "aux.enhance":
        assert meta["execution_id"] == "x1" and result["reference_cues"] == [{"index": 0, "cue": "note"}]
    if op == "aux.compare":
        assert result["checks"] == {"change_1": True}  # labels reach the engine: source != candidate


def test_aux_single_image_ops_validate_before_taking_the_gpu(rig: Rig) -> None:
    params, _ = AUX_CASES["aux.qa"]
    two = [(png(1), "image", ""), (png(2), "image", "")]
    with pytest.raises(ExecutionFailed) as e:
        rig.run(make_offer("aux.qa", params, two), [b for b, _, _ in two])
    assert e.value.code == "input_invalid" and rig.state.next_epoch("gpu1") == 1  # no handoff was spent


def test_aux_ownership_unknown_blocks_with_admission_rejected(rig: Rig) -> None:
    rig.aux.unload_response = AckError("worker still busy")
    params, inputs = AUX_CASES["aux.qa"]
    with pytest.raises(ExecutionBlocked) as e:
        rig.run(make_offer("aux.qa", params, inputs), [b for b, _, _ in inputs])
    assert e.value.code == "admission_rejected" and rig.ex.lanes["gpu1"].state == "unknown"


# -- worker3d and lane handoff --------------------------------------------------------------------------------------

GEN = {"execution_id": "w1", "op": "generate", "params": {"seed": 5, "pipeline_type": "512"}}


def test_lane_handoff_persists_epochs_across_state_reopen(rig: Rig) -> None:
    qa, qa_in = AUX_CASES["aux.qa"]
    body = png()
    rig.run(make_offer("aux.qa", qa, qa_in), [b for b, _, _ in qa_in])
    rig.run(make_offer("worker3d.generate", GEN, [(body, "body", "")]), [body])
    rig.run(make_offer("aux.qa", qa, qa_in), [b for b, _, _ in qa_in])
    lane = rig.ex.lanes["gpu1"]
    assert (lane.owner, lane.epoch, lane.grants) == ("aux", 3, 3)
    assert rig.w3d.calls == ["unload", "generate", "unload"]
    rig.state.close()
    assert RunnerState(rig.config.state_dir).next_epoch("gpu1") == 4
    assert RunnerState(rig.config.state_dir).next_epoch("gpu0") == 1  # epochs are per slot


def test_worker3d_result_and_ack_only_after_spooled(rig: Rig) -> None:
    body = png()
    offer = make_offer("worker3d.generate", GEN, [(body, "body", "")])
    outputs, meta = rig.run(offer, [body])
    raw = by_name(outputs)["result.bin"]
    assert raw[0].startswith(b"SIMULATED-RAW:") and raw[1] == "application/octet-stream"
    assert meta["execution_id"] == "w1" and meta["simulated"] is True
    assert "w1" in rig.w3d.executions  # the worker keeps the result until the manifest is durable
    rig.ex.spooled(offer)
    assert "w1" not in rig.w3d.executions
    rig.ex.spooled(make_offer("image.t2i", T2I))  # other operations: no-op


def test_worker3d_export_and_failure_mapping(rig: Rig) -> None:
    body = png()
    raw = by_name(rig.run(make_offer("worker3d.generate", GEN, [(body, "body", "")]), [body])[0])["result.bin"][0]
    exp = {"execution_id": "w2", "op": "export",
           "params": {"exporter": "clean", "decimation_target": 500, "texture_size": 64, "remesh": False}}
    glb = by_name(rig.run(make_offer("worker3d.export", exp, [(raw, "body", "")]), [raw])[0])["result.bin"][0]
    assert glb[:4] == b"glTF"
    rig.w3d.fail_ops["generate"] = "oom"
    with pytest.raises(ExecutionFailed) as e:
        rig.run(make_offer("worker3d.generate", {**GEN, "execution_id": "w3"}, [(body, "body", "")]), [body])
    assert e.value.code == "oom"
    mismatched = {**exp, "execution_id": "w4"}
    with pytest.raises(ExecutionFailed) as e:  # op does not match the operation name
        rig.run(make_offer("worker3d.generate", mismatched, [(raw, "body", "")]), [raw])
    assert e.value.code == "input_invalid"
    with pytest.raises(ExecutionFailed) as e:  # the worker rejects a non-raw intermediate
        rig.run(make_offer("worker3d.export", {**exp, "execution_id": "w5"}, [(body, "body", "")]), [body])
    assert e.value.code == "input_invalid"


def test_worker3d_lost_and_still_running(rig: Rig) -> None:
    body = png()
    offer = make_offer("worker3d.generate", GEN, [(body, "body", "")])
    rig.w3d.hold = True
    with pytest.raises(ExecutionBlocked) as b:  # uncertain: reconcile later with the SAME execution id
        rig.run(offer, [body])
    assert b.value.code == "node_unavailable"
    rig.w3d.restart()
    with pytest.raises(ExecutionFailed) as e:
        rig.run(offer, [body])
    assert e.value.code == "internal" and e.value.lost is True


# -- barrier --------------------------------------------------------------------------------------------------------


def test_barrier_aux3d_reset_ready_or_unknown(rig: Rig) -> None:
    assert recover_slots(rig.config, rig.ex, rig.state, only={"gpu1"}) == {"gpu1": "ready"}
    assert rig.state.next_epoch("gpu1") == 2  # the reset consumed a persisted epoch
    assert rig.aux.calls.count("unload") == 1 and rig.w3d.calls.count("unload") == 1
    rig.aux.unload_response = AckError("refuses")
    states = recover_slots(rig.config, rig.ex, rig.state)
    assert states["gpu1"] == "unknown" and rig.ex.lanes["gpu1"].state == "unknown"
    rig.aux.unload_response = None
    assert recover_slots(rig.config, rig.ex, rig.state, only={"gpu1"}) == {"gpu1": "ready"}


def test_barrier_image_slot(rig: Rig) -> None:
    assert recover_slots(rig.config, rig.ex, rig.state, only={"gpu0"}) == {"gpu0": "ready"}
    rig.comfy.queue_ids = {"foreign"}
    assert recover_slots(rig.config, rig.ex, rig.state, only={"gpu0"}) == {"gpu0": "unknown"}
    local = make_offer("image.t2i", {**T2I, "prompt_id": "mine"})
    rig.state.record_attempt(local)  # admitted: owned by this runner
    rig.comfy.queue_ids = {"mine"}
    assert recover_slots(rig.config, rig.ex, rig.state, only={"gpu0"}) == {"gpu0": "ready"}
    rig.comfy.down = True
    assert recover_slots(rig.config, rig.ex, rig.state, only={"gpu0"}) == {"gpu0": "unreachable"}


def test_barrier_without_engines_is_ready(rig: Rig) -> None:
    assert recover_slots(rig.config, FakeExecutor(), rig.state) == {"gpu0": "ready", "gpu1": "ready"}


# -- receipts -------------------------------------------------------------------------------------------------------


def _entry(root: Path, name: str, data: bytes | None, *, service: str = "aux", sha: str | None | bool = True,
           size: int | None = None) -> dict[str, Any]:
    if data is not None:
        (root / name).mkdir(parents=True)
        (root / name / "w.bin").write_bytes(data)
    digest = None if sha is None else (hashlib.sha256(b"right").hexdigest() if sha is True else str(sha))
    return {"repo": f"org/{name}", "revision": "rev1", "local_dir": name, "service": service,
            "files": {"w.bin": {"size": len(b"right") if size is None else size, "sha256": digest}}}


def _catalog(root: Path) -> dict[str, Any]:
    return {"schema_version": 1, "models": {
        "ok": _entry(root, "ok", b"right"), "missing": _entry(root, "missing", None),
        "corrupt": _entry(root, "corrupt", b"wrong"), "unpinned": _entry(root, "unpinned", b"right", sha=None),
        "other_service": _entry(root, "other", b"right", service="worker3d")}}


def test_receipts_full_verification_statuses(tmp_path: Path) -> None:
    root = tmp_path / "models"
    catalog = _catalog(root)
    sha = sha256_json(catalog)
    got = {r.key: r for r in model_receipts(catalog, sha, root, {"aux"}, HashCache(tmp_path / "h.json"))}
    assert {k: r.status for k, r in got.items()} == {"ok": "ok", "missing": "missing", "corrupt": "corrupt",
                                                    "unpinned": "unpinned"}
    assert all(r.verification == "full" and r.catalog_sha256 == sha and r.revision == "rev1" for r in got.values())
    both = model_receipts(catalog, sha, root, {"aux", "worker3d"}, HashCache(tmp_path / "h2.json"))
    assert len(both) == 5
    sim = model_receipts(catalog, sha, root, {"aux"}, None, simulated=True)
    assert {r.status for r in sim} == {"ok"}  # simulated: no disk access at all


def test_receipts_reuse_hash_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "models"
    catalog = {"models": {"ok": _entry(root, "ok", b"right")}}
    path = tmp_path / "h.json"
    assert model_receipts(catalog, "c" * 64, root, {"aux"}, HashCache(path))[0].status == "ok"

    def boom(*_a: Any) -> None:
        raise AssertionError("file was hashed again")

    monkeypatch.setattr(models_mod, "hashlib", SimpleNamespace(sha256=boom))
    assert model_receipts(catalog, "c" * 64, root, {"aux"}, HashCache(path))[0].status == "ok"  # pure cache hit


def test_session_receipts_verify_once_per_catalog_when_all_ok(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config(tmp_path).model_copy(update={"simulated": False, "models_root": tmp_path / "models"})
    catalog = {"models": {"ok": _entry(cfg.models_root, "ok", b"right")}}  # type: ignore[arg-type]
    state = RunnerState(cfg.state_dir)
    first = session_receipts(cfg, state, catalog, "d" * 64)
    assert [r.status for r in first] == ["ok"] and state.get_identity(f"receipts:{'d' * 64}")
    monkeypatch.setattr(receipts_mod, "verify_model", lambda *a, **k: pytest.fail("re-verified"))
    assert session_receipts(cfg, state, catalog, "d" * 64) == first
    bad = {"models": {"m": _entry(cfg.models_root, "gone", None)}}  # type: ignore[arg-type]
    monkeypatch.undo()
    assert session_receipts(cfg, state, bad, "e" * 64)[0].status == "missing"
    assert state.get_identity(f"receipts:{'e' * 64}") is None  # failures are re-checked next session


def test_files_sha256_prefix_is_the_studio_residency_identity() -> None:
    lock = load_lock(ROOT / "config")
    receipts = model_receipts(lock, sha256_json(lock), None, {"aux", "comfyui", "worker3d"}, None, simulated=True)
    assert {r.key for r in receipts} == set(lock["models"])
    for r in receipts:
        entry = lock["models"][r.key]  # the blob Studio's coordinator/stages/base.py `_identity` hashes
        blob = json.dumps({"repo": entry.get("repo"), "revision": entry.get("revision"),
                           "files": entry.get("files")}, sort_keys=True, default=str)
        assert r.files_sha256[:12] == hashlib.sha256(blob.encode()).hexdigest()[:12]
        assert r.files_sha256 == lock_identity(entry)

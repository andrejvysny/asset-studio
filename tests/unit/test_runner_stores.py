"""Journal v4 + RunnerStore + AttemptStore: migration, idempotency, conditional transitions, uploads."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from assetstudio_server.attemptstore import StaleRevision
from assetstudio_server.journal import EXECUTION_MODE_KEY, Journal, JournalTooNew
from assetstudio_server.runnerstore import DeviceConflict

V4_TABLES = ("journal_meta", "runner_sessions", "devices", "slots", "attempts", "attempt_events", "uploads")


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    j = Journal(tmp_path / "j" / "operations.sqlite")
    yield j
    j.close()


def _tables(j: Journal) -> set[str]:
    return {r[0] for r in j._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _version(j: Journal) -> int:
    return int(j._db.execute("PRAGMA user_version").fetchone()[0])


def test_v4_fresh_and_upgrade_from_v3(tmp_path: Path) -> None:
    path = tmp_path / "operations.sqlite"
    j = Journal(path)
    assert set(V4_TABLES) <= _tables(j) and _version(j) == 4
    for t in V4_TABLES:
        j._db.execute(f"DROP TABLE {t}")
    j._db.execute("PRAGMA user_version=3")
    j.close()
    j = Journal(path)
    assert set(V4_TABLES) <= _tables(j) and _version(j) == 4
    j.close()


def test_journal_too_new(tmp_path: Path) -> None:
    path = tmp_path / "operations.sqlite"
    Journal(path).close()
    import sqlite3
    c = sqlite3.connect(path)
    c.execute("PRAGMA user_version=99")
    c.close()
    with pytest.raises(JournalTooNew, match="v99"):
        Journal(path)


def test_meta(journal: Journal) -> None:
    assert journal.meta_get(EXECUTION_MODE_KEY) is None
    journal.meta_set(EXECUTION_MODE_KEY, "remote")
    journal.meta_set(EXECUTION_MODE_KEY, "direct")
    assert journal.meta_get(EXECUTION_MODE_KEY) == "direct"


def test_sessions_idempotent_and_supersede(journal: Journal) -> None:
    rs = journal.runners
    a, created = rs.open_session("rnr_a", "boot1", 1, "poll", {"os": "linux"}, {"v": "1"})
    assert created and a["state"] == "active" and a["platform"] == {"os": "linux"}
    again, created = rs.open_session("rnr_a", "boot1", 1, "poll", {}, {})
    assert not created and again["id"] == a["id"]
    b, created = rs.open_session("rnr_a", "boot2", 1, "poll", {}, {})
    assert created and rs.get_session(a["id"])["state"] == "superseded"  # type: ignore[index]
    assert rs.active_session("rnr_a")["id"] == b["id"]  # type: ignore[index]
    assert not rs.touch_session(a["id"]) and rs.touch_session(b["id"], lifecycle="draining")
    assert rs.get_session(b["id"])["lifecycle"] == "draining"  # type: ignore[index]
    assert [s["id"] for s in rs.sessions("rnr_a", ("active",))] == [b["id"]]


def test_inventory_monotonic(journal: Journal) -> None:
    rs = journal.runners
    s, _ = rs.open_session("rnr_a", "b", 1, "poll", {}, {})
    assert rs.set_inventory(s["id"], 0, {"x": 1})
    assert not rs.set_inventory(s["id"], 0, {"x": 2})
    assert rs.set_inventory(s["id"], 3, {"x": 3})
    assert not rs.set_inventory(s["id"], 2, {"x": 4})
    assert rs.get_session(s["id"])["inventory"] == {"x": 3}  # type: ignore[index]
    rs.open_session("rnr_a", "b2", 1, "poll", {}, {})
    assert not rs.set_inventory(s["id"], 9, {})


def _dev(uuid: str, index: int = 0) -> dict[str, Any]:
    return {"uuid": uuid, "index": index, "name": "GPU", "memory_mb": 8000, "fallback": False}


def test_claim_devices(journal: Journal) -> None:
    rs = journal.runners
    rs.claim_devices("rnr_a", [_dev("GPU-a")])
    rs.claim_devices("rnr_b", [_dev("GPU-b")])  # same index, different uuid
    with pytest.raises(DeviceConflict) as e:
        rs.claim_devices("rnr_b", [_dev("GPU-new", 1), _dev("GPU-a", 2)])
    assert e.value.owner == "rnr_a" and e.value.code == "forbidden_scope"
    assert {d["uuid"] for d in rs.devices()} == {"GPU-a", "GPU-b"}  # nothing written
    rs.claim_devices("rnr_a", [_dev("GPU-a", 5)])  # own refresh keeps claim state
    assert rs.devices("rnr_a")[0]["idx"] == 5
    assert rs.retire_device("GPU-a")
    rs.claim_devices("rnr_b", [_dev("GPU-a")])
    assert rs.devices("rnr_b")[0]["runner_id"] == "rnr_b"


def test_device_claim_transitions(journal: Journal) -> None:
    rs = journal.runners
    rs.claim_devices("rnr_a", [_dev("GPU-a")])
    assert rs.reserve_device("GPU-a", "atp_1") and not rs.reserve_device("GPU-a", "atp_2")
    assert not rs.release_device("GPU-a", "atp_2")
    assert rs.mark_device_uncertain("GPU-a") and not rs.mark_device_uncertain("GPU-a")
    rs.claim_devices("rnr_a", [_dev("GPU-a")])
    assert rs.devices()[0]["claim"] == "uncertain"
    assert rs.release_device("GPU-a", "atp_1")
    d = rs.devices()[0]
    assert d["claim"] == "free" and d["claim_attempt"] is None
    assert not rs.mark_device_uncertain("GPU-a")
    assert rs.retire_device("GPU-a") and not rs.retire_device("GPU-a")
    assert not rs.reserve_device("GPU-a", "atp_3")


def test_replace_slots(journal: Journal) -> None:
    rs = journal.runners
    slot = {"slot_id": "s0", "capability": "image", "device_uuids": ["GPU-a"], "engines": ["comfy"],
            "loaded_residency": None, "state": "idle"}
    rs.replace_slots("rnr_a", [slot, {**slot, "slot_id": "s1"}])
    rs.replace_slots("rnr_b", [slot])
    rs.replace_slots("rnr_a", [{**slot, "slot_id": "s2", "loaded_residency": "sdxl"}])
    got = rs.slots("rnr_a")
    assert [s["slot_id"] for s in got] == ["s2"] and got[0]["device_uuids"] == ["GPU-a"]
    assert got[0]["engines"] == ["comfy"] and len(rs.slots()) == 2


def _offer(j: Journal, *, gen: int = 1, digest: str = "d1") -> tuple[dict[str, Any], bool]:
    return j.attempts.create_offer(
        task_id="stk_1", call_key="gen", generation=gen, project_id="prj_1", operation="image.generate",
        operation_version=1, input_digest=digest, offer={"a": 1}, runner_id="rnr_a", session_id="rse_1",
        slot_id="s0", offer_expires_at="2030-01-01T00:00:00.000Z")


def test_create_offer_idempotent_and_stale(journal: Journal) -> None:
    at = journal.attempts
    a, created = _offer(journal)
    assert created and a["state"] == "offered" and a["offer"] == {"a": 1}
    assert a["id"] == at.attempt_id_for("stk_1", "gen", 1)
    b, created = _offer(journal)
    assert not created and b["id"] == a["id"]
    c, created = _offer(journal, gen=2)
    assert created and [x["generation"] for x in at.for_call("stk_1", "gen")] == [1, 2]
    assert at.latest("stk_1", "gen")["id"] == c["id"]  # type: ignore[index]
    with pytest.raises(StaleRevision):
        _offer(journal, gen=3, digest="other")
    assert [e["event"] for e in at.events(a["id"])] == ["offered"]
    assert len(at.list(states=("offered",), runner_id="rnr_a", project_id="prj_1", task_id="stk_1")) == 2
    assert at.list(runner_id="rnr_zzz") == []


def test_transition_conditional_and_events(journal: Journal) -> None:
    at = journal.attempts
    a, _ = _offer(journal)
    aid = a["id"]
    assert not at.transition(aid, ("leased",), "admitted")
    assert at.transition(aid, ("offered",), "leased", lease_until="2030-01-01T00:00:00.000Z", detail={"x": 1})
    assert at.transition(aid, ("leased",), "failed", event="report", error={"code": "boom"},
                         progress={"p": 1}, manifest={"files": []})
    row = at.get(aid)
    assert row and row["state"] == "failed" and row["error"] == {"code": "boom"} and row["revision"] == 2
    assert row["manifest"] == {"files": []} and row["progress"] == {"p": 1}
    ev = at.events(aid)
    assert [e["event"] for e in ev] == ["offered", "leased", "report"] and ev[1]["detail"] == {"x": 1}
    with pytest.raises(ValueError):
        at.transition(aid, ("failed",), "lost", bogus=1)
    assert len(at.events(aid)) == 3


def test_control_and_disposition(journal: Journal) -> None:
    at = journal.attempts
    a, _ = _offer(journal)
    aid = a["id"]
    with pytest.raises(ValueError):
        at.set_control(aid, "pause")
    assert at.set_control(aid, "cancel") and at.get(aid)["control"] == "cancel"  # type: ignore[index]
    assert at.transition(aid, ("offered",), "cancelled")
    assert not at.set_control(aid, "run")
    assert at.pending_receipts("rnr_a") == []
    assert at.set_disposition(aid, "cancelled") and not at.set_disposition(aid, "committed")
    row = at.get(aid)
    assert row and row["disposition"] == "cancelled" and row["disposition_at"]
    assert [p["id"] for p in at.pending_receipts("rnr_a")] == [aid] and at.pending_receipts("rnr_b") == []
    assert at.mark_receipts_delivered([aid]) == 1 and at.mark_receipts_delivered([]) == 0
    assert at.pending_receipts("rnr_a") == []
    assert at.events(aid)[-1]["event"] == "disposition"


def _upload(j: Journal, sha: str = "a" * 64, size: int = 100, expires: str = "2030-01-01T00:00:00.000Z"
            ) -> tuple[dict[str, Any], bool]:
    return j.attempts.create_upload(attempt_id="atp_1", generation=1, project_id="prj_1", runner_id="rnr_a",
                                    sha256=sha, size=size, role="output", mime="image/png", chunk_size=50,
                                    expires_at=expires)


def test_uploads(journal: Journal) -> None:
    at = journal.attempts
    u, created = _upload(journal)
    assert created and u["state"] == "open" and u["reserved_bytes"] == 100 and u["id"].startswith("xfr_")
    again, created = _upload(journal)
    assert not created and again["id"] == u["id"]
    assert at.record_chunk(u["id"], 0, "c0") == "stored"
    assert at.record_chunk(u["id"], 0, "c0") == "duplicate"
    assert at.record_chunk(u["id"], 0, "zz") == "conflict"
    assert at.get_upload(u["id"])["received"] == {"0": "c0"}  # type: ignore[index]
    u2, _ = _upload(journal, sha="b" * 64, size=40)
    assert at.reserved_bytes() == 140 and at.reserved_bytes(project_id="prj_x") == 0
    assert at.reserved_bytes(runner_id="rnr_a") == 140
    assert at.set_upload_state(u["id"], ("open",), "finalizing")
    assert at.record_chunk(u["id"], 1, "c1") == "closed" and at.record_chunk("xfr_none", 0, "c") == "closed"
    assert at.reserved_bytes() == 140
    assert at.set_upload_state(u["id"], ("finalizing",), "finalized", finalized_at="2030-01-01T00:00:00.000Z")
    assert not at.set_upload_state(u["id"], ("finalizing",), "expired")
    done = at.get_upload(u["id"])
    assert done and done["reserved_bytes"] == 0 and done["finalized_at"] and at.reserved_bytes() == 40
    assert at.expired_uploads("2029-01-01T00:00:00.000Z") == []
    assert [x["id"] for x in at.expired_uploads("2031-01-01T00:00:00.000Z")] == [u2["id"]]
    assert at.set_upload_state(u2["id"], ("open",), "expired") and at.reserved_bytes() == 0

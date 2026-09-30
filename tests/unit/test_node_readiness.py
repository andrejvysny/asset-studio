"""Node-mode readiness derived from runner inventories (R10, R13): receipts, slots, labels. Service level only."""
from __future__ import annotations

from typing import Any

from assetstudio_server.execution import NodeBackend
from assetstudio_server.services import node_readiness as nr
from assetstudio_server.services.placement import catalog_sha256

from tests.unit.test_runner_services import MODEL, Handle, World, w  # noqa: F401 - `w` is the world fixture


def _put(h: Handle, models: list[str] | None = None, revision: int = 1, *, status: str = "ok",
         labels: list[str] | None = None) -> None:
    inv = h.inventory(models, revision)
    receipts = [m.model_copy(update={"status": status}) for m in inv.models]
    h.put_inventory(inv=inv.model_copy(update={"models": receipts, "labels": labels or []}))


def test_one_ok_receipt_makes_the_model_ready_and_names_the_others(w: World) -> None:  # noqa: F811
    good, bad = w.runner("good", models=[MODEL]), w.runner("bad")
    _put(bad, [MODEL], status="corrupt")
    st = nr.node_model_statuses(w.studio)[MODEL]
    assert st.ready and st.status == "ok" and st.full_verified and "good" in st.detail
    bad.put_inventory(inv=bad.inventory([MODEL], 2).model_copy(update={"models": []}))
    assert good.id and nr.node_model_statuses(w.studio)["qwen_image_2512"].status == "missing"


def test_corrupt_outranks_missing_and_details_name_runners(w: World) -> None:  # noqa: F811
    a, b = w.runner("a"), w.runner("b")
    _put(a, [MODEL], status="corrupt")
    _put(b, [MODEL], status="missing")
    st = nr.node_model_statuses(w.studio)[MODEL]
    assert not st.ready and st.status == "corrupt"
    assert "runner a" in st.detail and "runner b" in st.detail


def test_no_runner_means_missing_with_reason(w: World) -> None:  # noqa: F811
    st = nr.node_model_statuses(w.studio)[MODEL]
    assert st.status == "missing" and st.detail == "no connected runner verified this model"


def test_stale_session_is_ignored(w: World) -> None:  # noqa: F811
    w.runner("r", models=[MODEL])
    assert nr.node_model_statuses(w.studio)[MODEL].ready and len(nr.fresh_inventories(w.studio)) == 1
    w.studio.settings.runner_lease_s = 0
    assert nr.fresh_inventories(w.studio) == []
    assert not nr.node_model_statuses(w.studio)[MODEL].ready
    assert nr.operation_readiness(w.studio, "aux.enhance") == (False, [nr.NO_RUNNER])


def test_catalog_sha_mismatch_is_not_ok(w: World) -> None:  # noqa: F811
    h = w.runner("old")
    h.put_inventory(inv=h.inventory([MODEL], 1, sha="e" * 64))
    st = nr.node_model_statuses(w.studio)[MODEL]
    assert not st.ready and "another catalog" in st.detail
    assert catalog_sha256(w.studio) != "e" * 64


def test_operation_readiness_reasons(w: World) -> None:  # noqa: F811
    assert nr.operation_readiness(w.studio, "image.t2i") == (False, [nr.NO_RUNNER])
    h = w.runner("r")
    assert nr.operation_readiness(w.studio, "image.t2i") == (True, [])
    ok, reasons = nr.operation_readiness(w.studio, "worker3d.generate")  # the fixture runner has no 3D engine
    assert not ok and any("wrong capability" in r or "no engine offers" in r for r in reasons)
    ok, reasons = nr.operation_readiness(w.studio, "aux.cutout")  # aux slot exists but does not offer cutout
    assert not ok and any("runner r slot aux: no engine offers aux.cutout" in r for r in reasons)
    w.studio.journal.runners.replace_slots(h.id, [{**s, "state": "unreachable"} for s in
                                                  w.studio.journal.runners.slots(h.id)])
    ok, reasons = nr.operation_readiness(w.studio, "aux.enhance")
    assert not ok and any("slot state unreachable" in r for r in reasons)


def test_occupancy_does_not_make_an_operation_unready(w: World) -> None:  # noqa: F811
    h = w.runner("r")
    w.leased(h)  # the aux slot is bound to an attempt and its device is reserved
    assert nr.operation_readiness(w.studio, "aux.enhance") == (True, [])


def test_nodes_simulated_all_none_mixed(w: World) -> None:  # noqa: F811
    assert nr.nodes_simulated(w.studio) is False  # no runner
    a, b = w.runner("a"), w.runner("b")
    _put(a, labels=["gpu"])
    _put(b, labels=["gpu"])
    assert nr.nodes_simulated(w.studio) is False
    _put(a, revision=2, labels=["simulated"])
    assert nr.nodes_simulated(w.studio) is False  # mixed
    _put(b, revision=2, labels=["simulated"])
    assert nr.nodes_simulated(w.studio) is True
    assert NodeBackend(w.studio).simulated is True


def test_exporters_follow_slots_and_the_research_label(w: World) -> None:  # noqa: F811
    h = w.runner("r")
    assert nr.node_exporters(w.studio) == {"clean": False, "research": False}  # no worker3d.export slot
    inv = h.inventory(None, 1)
    ops = [{"op": "worker3d.generate", "version": 1}, {"op": "worker3d.export", "version": 1}]
    aux: dict[str, Any] = inv.model_dump()["slots"][1]
    slot = {**aux, "engines": [{"engine": "worker3d", "version": "1", "operations": ops}]}
    with_3d = inv.model_copy(update={"slots": [inv.slots[0], type(inv.slots[1]).model_validate(slot)]})
    h.put_inventory(inv=with_3d)
    assert nr.node_exporters(w.studio) == {"clean": True, "research": False}
    h.put_inventory(inv=with_3d.model_copy(update={"revision": 2, "labels": ["exporter-research"]}))
    assert nr.node_exporters(w.studio) == {"clean": True, "research": True}
    assert NodeBackend(w.studio).worker3d().health()["exporters"] == {"clean": True, "research": True}  # type: ignore[union-attr]

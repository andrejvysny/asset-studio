"""Runner/Studio protocol DTOs: validation, digests, inventory rules, transfer math."""
from __future__ import annotations

import base64
import uuid
from typing import Any

import pytest
from assetstudio_core.ids import new_id
from assetstudio_protocol.execution import (
    AttemptReport,
    FileRef,
    InputRef,
    Offer,
    Policy,
    Requirements,
    ResultManifest,
    compute_input_digest,
)
from assetstudio_protocol.inventory import Inventory
from assetstudio_protocol.runners import Heartbeat, SessionHello, StudioKey, challenge_message
from assetstudio_protocol.transfer import MIB, chunk_count, chunk_range
from assetstudio_protocol.versions import negotiate
from pydantic import ValidationError

H = "a" * 64
B64_32 = base64.b64encode(bytes(32)).decode()


def make_offer(**over: Any) -> Offer:
    inputs = over.pop("inputs", [InputRef(sha256=H, size=3, role="source", mime="image/png")])
    params = over.pop("params", {"prompt": "a cat", "seed": 1})
    op = over.pop("operation", "image.edit")
    req = Requirements(capability="image", engine="comfyui", models=["flux@abc123def456"])
    digest = compute_input_digest(op, 1, inputs, params, req, Policy())
    fields: dict[str, Any] = dict(
        schema="assetstudio.execution.v1", attempt_id=new_id("atp"), task_id="t1", call_key="job/1:0",
        generation=1, runner_id=new_id("rnr"), session_id=new_id("rse"), slot_id="gpu0", operation=op,
        operation_version=1, input_digest=digest, inputs=inputs, params=params, requirements=req,
        offer_expires_at="2026-01-01T00:00:00Z")
    fields.update(over)
    return Offer(**fields)


def dev(u: str, i: int = 0) -> dict[str, Any]:
    return {"uuid": u, "index": i, "name": "GPU", "memory_mb": 24000}


def slot(sid: str, uuids: list[str], cap: str = "image", engine: str = "comfyui") -> dict[str, Any]:
    return {"slot_id": sid, "capability": cap, "device_uuids": uuids, "state": "ready",
            "engines": [{"engine": engine, "version": "1", "operations": [{"op": "image.t2i", "version": 1}]}]}


def inv(devices: list[dict[str, Any]], slots: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema": "assetstudio.runner.inventory.v1", "revision": 1, "observed_at": "2026-01-01T00:00:00Z",
            "devices": devices, "slots": slots}


def hello() -> SessionHello:
    return SessionHello(schema="assetstudio.runner.session.v1", runner_id=new_id("rnr"), boot_id=str(uuid.uuid4()),
                        protocol_versions=[1], software={"runner": "0.1"}, dispatch="pull",
                        platform={"os": "linux", "arch": "x86_64", "hostname": "h"})


def test_round_trips() -> None:
    offer = make_offer()
    assert Offer.model_validate(offer.model_dump()) == offer
    assert offer.model_dump()["schema"] == "assetstudio.execution.v1"
    h = hello()
    assert SessionHello.model_validate(h.model_dump()) == h
    i = Inventory.model_validate(inv([dev("GPU-1")], [slot("s1", ["GPU-1"])]))
    assert Inventory.model_validate(i.model_dump()) == i
    hb = Heartbeat(session_id=new_id("rse"), inventory_revision=0,
                   attempts=[AttemptReport(attempt_id=new_id("atp"), generation=1, state="executing")])
    assert Heartbeat.model_validate(hb.model_dump()) == hb
    rm = ResultManifest(schema="assetstudio.result.v1", attempt_id=new_id("atp"), generation=1,
                        files=[FileRef(name="a.png", sha256=H, size=1, mime="image/png")])
    assert ResultManifest.model_validate(rm.model_dump()) == rm


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        Heartbeat(session_id=new_id("rse"), inventory_revision=0, bogus=1)  # type: ignore[call-arg]


def test_negotiate() -> None:
    assert negotiate({1, 2}) == 1
    assert negotiate({2}) is None


def test_input_digest_stability_and_order() -> None:
    a = InputRef(sha256="a" * 64, size=1, role="r", mime="x/y")
    b = InputRef(sha256="b" * 64, size=1, role="r", mime="x/y")
    assert make_offer(inputs=[a, b]).input_digest == make_offer(inputs=[a, b]).input_digest
    assert make_offer(inputs=[a, b]).input_digest != make_offer(inputs=[b, a]).input_digest
    assert make_offer(params={"x": 1}).input_digest != make_offer(params={"x": 2}).input_digest


def test_offer_tamper_rejected() -> None:
    with pytest.raises(ValidationError, match="input_digest"):
        make_offer(input_digest="c" * 64)
    with pytest.raises(ValidationError, match="operation_version"):
        make_offer(operation_version=2)
    with pytest.raises(ValidationError, match="capability"):
        make_offer(operation="aux.qa")


def test_inventory_rules() -> None:
    devs = [dev("A"), dev("B", 1)]
    with pytest.raises(ValidationError, match="overlap"):
        Inventory.model_validate(inv(devs, [slot("s1", ["A"]), slot("s2", ["A"])]))
    with pytest.raises(ValidationError, match="unknown device"):
        Inventory.model_validate(inv(devs, [slot("s1", ["Z"])]))
    with pytest.raises(ValidationError, match="comfyui"):
        Inventory.model_validate(inv(devs, [slot("s1", ["A"], engine="aux")]))
    with pytest.raises(ValidationError, match="slot_ids"):
        Inventory.model_validate(inv(devs, [slot("s1", ["A"]), slot("s1", ["B"])]))
    with pytest.raises(ValidationError, match="device uuids"):
        Inventory.model_validate(inv([dev("A"), dev("A", 1)], []))
    Inventory.model_validate(inv(devs, [slot("s1", ["A"]), slot("s2", ["B"], "aux3d", "worker3d")]))


def test_key_length_validation() -> None:
    StudioKey(key_id="k1", public_key=B64_32, status="current")
    for bad in (base64.b64encode(bytes(31)).decode(), "not base64!!"):
        with pytest.raises(ValidationError):
            StudioKey(key_id="k1", public_key=bad, status="current")


def test_challenge_message_deterministic() -> None:
    m = challenge_message("rnr_x", "n", "aud")
    assert m == challenge_message("rnr_x", "n", "aud")
    assert m == (b'{"aud":"aud","nonce":"n","runner_id":"rnr_x","schema":"assetstudio.runner.challenge.v1"}')


def test_chunks() -> None:
    assert chunk_count(2 * MIB, MIB) == 2
    assert chunk_count(2 * MIB + 1, MIB) == 3
    assert chunk_range(0, 2 * MIB, MIB) == (0, MIB)
    assert chunk_range(2, 2 * MIB + 1, MIB) == (2 * MIB, 2 * MIB + 1)
    for idx in (-1, 2):
        with pytest.raises(ValueError):
            chunk_range(idx, 2 * MIB, MIB)


def test_id_prefix_validation() -> None:
    with pytest.raises(ValidationError):
        make_offer(attempt_id=new_id("rnr"))
    with pytest.raises(ValidationError):
        Heartbeat(session_id=new_id("atp"), inventory_revision=0)

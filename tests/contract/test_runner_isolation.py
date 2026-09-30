"""Control-plane isolation (R14): saturated bulk transfers never starve heartbeat, accept or acquire."""
from __future__ import annotations

import hashlib

from assetstudio_protocol.transfer import UploadCreate

from tests.contract.test_runner_api import Env, Node, env  # noqa: F401 - `env` is a fixture


def test_saturated_transfers_get_503_while_control_routes_stay_up(env: Env) -> None:  # noqa: F811
    node = Node(env)
    node.boot()
    offer = node.lease()
    node.report(offer, "executing")
    data = b"result bytes"
    sha = hashlib.sha256(data).hexdigest()
    up = node.client.create_upload(UploadCreate(attempt_id=offer.attempt_id, generation=1, sha256=sha, size=len(data),
                                                role="result.bin", mime="application/octet-stream"))
    url, hdr = f"/uploads/{up.upload_id}/chunks/0", {"X-Chunk-Sha256": sha}
    input_url = f"/attempts/{offer.attempt_id}/inputs/{offer.inputs[0].sha256}"
    studio = env.api.studio
    cap = studio.settings.transfer_concurrency
    assert cap == 4
    from assetstudio_server.routers.runner_api import take_transfer_slot

    held = [take_transfer_slot(studio) for _ in range(cap)]
    assert all(h is not None for h in held)
    put = node.raw("PUT", url, content=data, headers=hdr)
    assert put.status_code == 503 and put.headers["retry-after"] == "1" and put.json()["code"] == "resource_exhausted"
    get = node.raw("GET", input_url)
    assert get.status_code == 503 and get.headers["retry-after"] == "1"
    hb = node.raw("POST", f"/sessions/{node.sid}/heartbeat", json={"session_id": node.sid, "inventory_revision": 1})
    assert hb.status_code == 200, hb.text
    assert node.acquire() is None  # an empty poll is fine; it was never refused
    held[0].release()  # one slot frees up: transfers work again and the slot is released after each request
    assert node.raw("PUT", url, content=data, headers=hdr).json() == {"status": "stored"}
    assert node.raw("GET", input_url).status_code == 200
    held[0] = take_transfer_slot(studio)
    assert held[0] is not None  # neither request leaked its slot
    for h in held:
        assert h is not None
        h.release()

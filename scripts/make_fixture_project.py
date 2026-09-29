"""Build a small SIMULATED fixture project with the code of whatever checkout is on PYTHONPATH.

Used to freeze projects written by older releases (a70232b, 60ff832) for migration tests (IM09/IM10):

    git worktree add /tmp/as-old a70232b
    PYTHONPATH=/tmp/as-old/packages:/tmp/as-old/services/studio uv run python scripts/make_fixture_project.py OUT

OUT receives `project/` (the portable project root) and `journal.sqlite` (a copy of the instance journal).
Every output is produced by the fake engines and labelled simulated.
"""
from __future__ import annotations

import io
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from assetstudio_server import studio as studio_mod
from assetstudio_server.adapters.fake import FakeAux, FakeEngine
from assetstudio_server.main import create_app
from assetstudio_server.settings import Settings
from fastapi.testclient import TestClient
from PIL import Image

H = {"x-assetstudio": "1"}
CATEGORIES = [
    {"id": "concept", "slug": "concept", "label": "Concept", "defaults": {"kind": "concept_art"}},
    {"id": "props", "slug": "props", "label": "Props", "defaults": {"kind": "model3d"}},
    {"id": "sprites", "slug": "sprites", "label": "Sprites", "defaults": {"kind": "sprite"}},
    {"id": "materials", "slug": "materials", "label": "Materials", "defaults": {"kind": "material"}},
]


class Client:
    def __init__(self, c: TestClient, st: Any) -> None:
        self.c, self.st = c, st

    def get(self, path: str) -> Any:
        r = self.c.get(path)
        assert r.status_code == 200, r.text
        return r.json()

    def post(self, path: str, body: Any) -> Any:
        r = self.c.post(path, json=body, headers=H)
        assert r.status_code in (200, 201, 202), r.text
        return r.json()

    def wait(self) -> None:
        end = time.monotonic() + 60
        while time.monotonic() < end:
            if not self.st.journal.list(states=("held", "queued", "running", "reconciling", "cancel_requested")):
                return
            time.sleep(0.05)
        raise SystemExit("operations did not finish")


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (48, 48), (120, 90, 30)).save(out, "PNG")
    return out.getvalue()


def _batch(a: Client, pid: str, cat: str, names: list[str], key: str, publish: int) -> None:
    """Enhance -> confirm -> approve all but the last item -> build -> accept + publish the first `publish`."""
    base = f"/api/v1/projects/{pid}/batches"
    bid = a.post(base, {"title": f"fixture {cat}", "category_id": cat, "idempotency_key": key, "candidate_count": 2,
                        "items": [{"name": n, "brief": f"brief for {n}"} for n in names]})["batch"]["id"]
    a.wait()
    items = a.get(f"{base}/{bid}")["items"]
    a.post(f"{base}/{bid}:confirm-and-generate", {"idempotency_key": f"{key}-c", "items": [
        {"item_id": i["id"], "prompt_revision_id": i["current_prompt"], "expected_item_revision": i["revision"]}
        for i in items]})
    a.wait()
    items = a.get(f"{base}/{bid}")["items"]
    decided = items[:-1] if len(items) > 1 else items
    for n, it in enumerate(decided):
        c = it["candidate_set"]["candidates"][0]
        a.post(f"{base}/{bid}:approve-candidates", {"idempotency_key": f"{key}-a{n}", "items": [{
            "item_id": it["id"], "expected_item_revision": it["revision"],
            "candidate_set_id": it["candidate_set"]["id"], "candidate_id": c["id"], "image_sha256": c["sha256"],
            "prompt_revision_id": it["candidate_set"]["prompt_revision_id"],
            "qa_evaluation_id": c["qa"]["id"] if c["qa"] else None, "override_qa": True}]})
    items = {i["id"]: i for i in a.get(f"{base}/{bid}")["items"]}
    ready = [i for i in items.values() if i["approval"] and i["legal"]["build"]]
    if not ready:
        return
    a.post(f"{base}/{bid}:build-approved", {"idempotency_key": f"{key}-b", "items": [
        {"item_id": i["id"], "approval_id": i["approval"], "expected_item_revision": i["revision"]} for i in ready]})
    a.wait()
    built = [i for i in a.get(f"{base}/{bid}")["items"] if i["build"] and i["build"]["result"] == "valid"][:publish]
    if not built:
        return
    a.post(f"{base}/{bid}:accept-builds", {"idempotency_key": f"{key}-acc", "items": [
        {"item_id": i["id"], "build_run_id": i["current_build"], "expected_item_revision": i["revision"]}
        for i in built]})
    prev = a.get(f"{base}/{bid}/publish-preview")["items"]
    a.post(f"{base}/{bid}:publish", {"idempotency_key": f"{key}-p", "items": [
        {"item_id": p["item_id"], "build_run_id": p["build_run_id"], "expected_item_revision": p["expected_item_revision"]}
        for p in prev]})
    a.wait()


def main(out: Path) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="as-fixture-"))
    s = Settings()
    s.instance_dir, s.project_roots = tmp / "instance", [tmp / "projects"]
    s.models_root, s.web_dir, s.engine, s.instance_id = tmp / "none", tmp / "none", "fake", "fixture"
    kwargs: dict[str, Any] = {"engine": FakeEngine(), "aux": FakeAux()}
    try:
        from assetstudio_server.adapters.fake import FakeWorker3d

        kwargs["worker3d"] = FakeWorker3d()
    except ImportError:
        pass
    st = studio_mod.build_studio(s, **kwargs)
    with TestClient(create_app(s, st)) as tc:
        a = Client(tc, st)
        pid = a.post("/api/v1/projects", {"name": "Fixture"})["id"]
        cfg = a.get(f"/api/v1/projects/{pid}/config")["config"]
        cfg["categories"] = CATEGORIES
        cfg["styles"] = {"house": {"label": "House", "guide": "painterly, warm light", "negative": "photo"}}
        cfg["defaults"]["style"] = "house"
        r = tc.patch(f"/api/v1/projects/{pid}/config", json={"expected_revision": 1, "config": cfg}, headers=H)
        assert r.status_code == 200, r.text
        _batch(a, pid, "concept", ["Tavern", "Harbour"], "fx-concept", publish=1)
        _batch(a, pid, "sprites", ["Knight", "Archer"], "fx-sprite", publish=1)
        _batch(a, pid, "materials", ["Cobble"], "fx-material", publish=0)
        # Before 60ff832 model3d builds were blocked: the batch then stops at approval (legacy recipe snapshot).
        _batch(a, pid, "props", ["Crate", "Barrel"], "fx-model3d", publish=1)
        imp = tc.post(f"/api/v1/projects/{pid}/imports:preview", files={"file": ("rock.png", _png(), "image/png")},
                      headers=H).json()
        a.post(f"/api/v1/projects/{pid}/imports:commit", {"import_id": imp["import_id"], "name": "Rock",
                                                          "kind": "concept_art", "idempotency_key": "fx-import-1"})
        root = Path(st.registry.get(pid).store.repo.root)  # type: ignore[attr-defined]
    st.close()
    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(root, out / "project", ignore=shutil.ignore_patterns("writer.lock", ".staging"))
    src = sqlite3.connect(s.instance_dir / "journal" / "operations.sqlite")
    dst = sqlite3.connect(out / "journal.sqlite")
    src.backup(dst)
    src.close()
    dst.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"fixture project {pid} -> {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))

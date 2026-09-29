"""IM09/IM10: projects written by a70232b and 60ff832 open unchanged; legacy production Batches read as Jobs."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from tests.conftest import Api

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "projects"


def _tree_hashes(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and p.parent.name != "_control"}


@pytest.mark.parametrize("commit", ["a70232b", "60ff832"])
def test_legacy_project_opens_and_jobs_read(make_api, tmp_path: Path, commit: str) -> None:
    api: Api = make_api()
    root = tmp_path / "projects" / commit
    shutil.copytree(FIXTURES / commit / "project", root)
    before = _tree_hashes(root)
    pid = api.post("/api/v1/projects:register", {"root": str(root)})["id"]
    jobs = api.get(f"/api/v1/projects/{pid}/batches")["batches"]
    assert len(jobs) == 4 and all(j["id"].startswith("bat_") for j in jobs)
    for j in jobs:
        d = api.get(f"/api/v1/projects/{pid}/batches/{j['id']}")
        assert d["items"] and all(i["job_id"] == j["id"] for i in d["items"])
    assets = api.get(f"/api/v1/projects/{pid}/assets")
    assert assets["total"] >= 3
    for a in assets["items"]:
        vs = api.get(f"/api/v1/projects/{pid}/assets/{a['asset_id']}/versions")
        assert vs
    # reading never rewrites historical records (immutable bytes, hashes preserved)
    after = _tree_hashes(root)
    assert {k: v for k, v in after.items() if k in before} == before


def test_legacy_model3d_snapshot_needs_explicit_fork(make_api, tmp_path: Path) -> None:
    """H03/IM09: a70232b recorded model3d.default v1 with other parameters; building it is refused, not guessed."""
    api: Api = make_api()
    root = tmp_path / "projects" / "old"
    shutil.copytree(FIXTURES / "a70232b" / "project", root)
    pid = api.post("/api/v1/projects:register", {"root": str(root)})["id"]
    job = next(j for j in api.get(f"/api/v1/projects/{pid}/batches")["batches"] if j["kind"] == "model3d")
    it = next(i for i in api.get(f"/api/v1/projects/{pid}/batches/{job['id']}")["items"] if i["approval"])
    r = api.raw("POST", f"/api/v1/projects/{pid}/batches/{job['id']}:build-approved", json={
        "idempotency_key": "legacy-build-1", "items": [{"item_id": it["id"], "approval_id": it["approval"],
                                                        "expected_item_revision": it["revision"]}]})
    assert r.status_code in (200, 202), r.text
    res = r.json()["results"][0]
    assert res["ok"] is False and res["code"] == "legacy_recipe" and "a70232b" in res["message"]
    assert r.json()["operation"] is None

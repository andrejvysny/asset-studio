"""Frozen legacy projects through the integration API: reads work, resolve is clean, no existing file changes."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from tests.conftest import Api
from tests.integration_support import client, integration_app_for, make_token

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "projects"
V1 = "/api/integration/v1"


def _tree_hashes(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and p.parent.name != "_control"}


@pytest.mark.parametrize("commit", ["a70232b", "60ff832"])
def test_legacy_project_reads_and_resolves_without_rewriting(make_api, tmp_path: Path, commit: str) -> None:
    api: Api = make_api()
    root = tmp_path / "projects" / commit
    shutil.copytree(FIXTURES / commit / "project", root)
    pid = api.post("/api/v1/projects:register", {"root": str(root)})["id"]
    app, fastapi_app = integration_app_for(api)
    c = client(app, make_token(app, "godot", ["assets:read"], [pid]))
    server_id = fastapi_app.state.identity.server_id
    before = _tree_hashes(root)
    listing = c.get(f"{V1}/libraries/{pid}/assets")
    assert listing.status_code == 200
    visible = {i["asset_id"] for i in listing.json()["items"]}
    refs, kinds = [], {}
    for a in api.get(f"/api/v1/projects/{pid}/assets")["items"]:
        kinds[a["asset_id"]] = a["kind"]
        for v in api.get(f"/api/v1/projects/{pid}/assets/{a['asset_id']}/versions")["versions"]:
            refs.append({"server_id": server_id, "library_id": pid, "asset_id": a["asset_id"],
                         "version_id": v["version_id"]})
    assert refs and visible == {a for a, k in kinds.items() if k == "model3d"}
    for chunk in (refs[i:i + 100] for i in range(0, len(refs), 100)):
        entries = c.post(f"{V1}/libraries/{pid}/resolve", json={"refs": chunk}).json()["entries"]
        for ref, e in zip(chunk, entries, strict=True):
            if kinds[ref["asset_id"]] != "model3d":
                assert e["state"] == "not_found" and e["error"]["code"] == "asset_not_found"
            else:
                assert e["state"] in ("ready", "unsupported")
            if e["state"] == "ready":
                assert c.get(f"{V1}/libraries/{pid}/assets/{ref['asset_id']}/versions/{ref['version_id']}"
                             "/descriptor").status_code == 200
    after = _tree_hashes(root)
    assert {k: v for k, v in after.items() if k in before} == before

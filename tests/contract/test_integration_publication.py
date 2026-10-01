"""Integration publication: preview (no mutation) -> atomic commit -> operation lookup (INT-SPEC §5.2/§5.4)."""
from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from assetstudio_core.canonical_v1 import canonical_bytes
from assetstudio_core.delivery import parse_descriptor
from assetstudio_core.domain import AssetVersion
from assetstudio_server.services import source_publications as sp
from assetstudio_storage.project import version_key

from tests.conftest import new_project
from tests.integration_publication_support import (
    FIXTURES,
    PubEnv,
    cluster_zip,
    commit_body,
    external_uri_glb,
    files_of,
    glb,
    glb_parts,
    make_env,
    patched_glb,
    source_parts,
)
from tests.integration_support import client, make_token

VALID = ["primitive_prop", "primitive_prop_v2", "textured_tree", "vertex_color_rock_with_collision", "csg_hut",
         "custom_shader_crystal"]
MUTATED = ("manifests", "versions", "publications", "names")


@pytest.fixture
def env(make_api) -> PubEnv:
    return make_env(make_api)


def _version(env: PubEnv, done: dict[str, Any]) -> AssetVersion:
    ref = done["asset_ref"]
    return env.ctx().store.get(version_key(ref["asset_id"], ref["version_id"]), AssetVersion)[0]


def _ver_url(env: PubEnv, done: dict[str, Any], suffix: str = "") -> str:
    ref = done["asset_ref"]
    return env.url(f"/assets/{ref['asset_id']}/versions/{ref['version_id']}{suffix}")


def test_scopes_and_libraries(env: PubEnv) -> None:
    ro = client(env.app, make_token(env.app, "ro", ["assets:read"], [env.lib]))
    other = new_project(env.api, "Other")
    foreign = client(env.app, make_token(env.app, "other", ["assets:publish"], [other]))
    body = {"preview_id": "ipv_0000000000000000", "portable_sha256": "0" * 64, "descriptor_draft_sha256": "0" * 64,
            "name": "x", "idempotency_key": "key-12345"}
    for c in (ro, foreign):
        assert c.post(env.url("/publications:preview"), files=files_of(glb_parts())).status_code == 403
        assert c.post(env.url("/publications:commit"), json=body).status_code == 403
        assert c.get(env.url("/publication-operations/key-12345")).status_code == 403
    assert env.c.get(env.url("/publication-operations/short")).status_code == 400


@pytest.mark.parametrize("name", VALID)
def test_preview_valid_fixture_has_no_side_effects(env: PubEnv, name: str) -> None:
    before = env.tree(*MUTATED)
    receipt = env.preview(source_parts(name))
    assert env.tree(*MUTATED) == before
    assert receipt["library_id"] == env.lib and receipt["token_name"] == "godot"
    assert receipt["source"]["manifest_sha256"] and receipt["package_sha256"] == receipt["parts"]["source"]["sha256"]
    assert receipt["bounds"]["min"] and receipt["budget"]["within_ipad_budget"] is True
    assert receipt["expires_at"] > receipt["created_at"]
    assert (env.api.studio.settings.instance_dir / "staging/integration" / receipt["preview_id"]).is_dir()
    assert not (env.api.studio.settings.instance_dir / "staging/integration" / receipt["preview_id"] / "source").exists()
    if name == "custom_shader_crystal":
        assert {"custom_shader_approximated", "shader_source_desktop_trust"} <= set(receipt["warnings"])


@pytest.mark.parametrize(("hostile", "code"), [("tres_script_property", "unsafe_package"),
                                               ("zip_bomb", "resource_limit"),
                                               ("glb_external_buffer", "unsafe_package")])
def test_hostile_source_is_rejected_and_staging_removed(env: PubEnv, hostile: str, code: str) -> None:
    zdata = (FIXTURES / "source_packages/hostile" / f"{hostile}.zip").read_bytes()
    parts = source_parts("primitive_prop", zip=zdata)
    r = env.c.post(env.url("/publications:preview"), files=files_of(parts))
    assert r.status_code == 422 and r.json()["error"]["code"] == code, r.text
    assert 0 < len(r.json()["error"]["details"]["problems"]) <= 20
    assert env.previews_on_disk() == []


@pytest.mark.parametrize("portable", [
    patched_glb(skins=[{"joints": [0]}]),
    patched_glb(animations=[{"channels": [], "samplers": []}]),
    external_uri_glb()])
def test_unsafe_portable_glb(env: PubEnv, portable: bytes) -> None:
    parts = {**glb_parts(), "portable": portable}
    r = env.c.post(env.url("/publications:preview"), files=files_of(parts))
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsafe_package", r.text
    assert env.previews_on_disk() == []


def test_draft_and_part_errors(env: PubEnv) -> None:
    good = source_parts("primitive_prop")

    def draft(**edit: Any) -> bytes:
        return canonical_bytes({**json.loads(good["descriptor"]), **edit})

    def rejected(parts: dict[str, bytes], status: int, detail: str | None) -> None:
        r = env.c.post(env.url("/publications:preview"), files=files_of(parts))
        assert r.status_code == status, r.text
        assert detail is None or r.json()["error"]["details"]["detail"] == detail
    rejected({**good, "descriptor": draft(placement_anchor=["9", "0", "0"])}, 422, "anchor_outside")
    slot = json.loads(good["descriptor"])["material_slots"][0]
    bad_surface = {**slot, "surfaces": {**slot["surfaces"], "portable_glb_v1": [{"mesh": 5, "primitive": 0}]}}
    rejected({**good, "descriptor": draft(material_slots=[bad_surface])}, 422, "surface_missing")
    rejected({**good, "descriptor": draft(footprint_radius_m="2")}, 422, "placement_mismatch")
    rejected({**good, "descriptor": draft(material_slots=[{**slot, "slot_id": "other"}])}, 422, "slot_mismatch")
    rejected({**good, "descriptor": b'{"schema_version":1,"footprint_radius_m":1.5}'}, 422, "draft_invalid")
    rejected({**good, "report": canonical_bytes({"portable_status": "exact", "omissions": ["x"],
                                                 "approximations": []})}, 422, "report_mismatch")
    rejected({k: v for k, v in good.items() if k != "report"}, 400, None)
    rejected({k: v for k, v in good.items() if k != "portable"}, 400, None)
    rejected({**good, "extra": b"x"}, 400, None)
    rejected({**good, "thumbnail": b"not a png"}, 422, "thumbnail_invalid")
    rejected({**glb_parts(), "descriptor": b" " * ((1 << 20) + 1)}, 413, None)
    assert env.previews_on_disk() == []


def test_commit_new_asset_end_to_end(env: PubEnv) -> None:
    parts = source_parts("textured_tree")
    receipt = env.preview(parts)
    cursor = env.c.get(f"{'/api/integration/v1'}/changes").json()["cursor"]
    done = env.commit(receipt, "publish-tree-1", tags=["tree"], licence="CC0-1.0", credit="me")
    assert done["operation"] == {"idempotency_key": "publish-tree-1", "op_id": done["operation"]["op_id"],
                                 "state": "committed"}
    ref = done["asset_ref"]
    assert ref["library_id"] == env.lib and ref["server_id"] == env.server_id and done["display_version"] == 1
    assert {d["representation"] for d in done["deliveries"]} == {"portable_glb_v1", "godot_static_source_v1"}
    version = _version(env, done)
    assert set(version.artifacts) == {"model", "preview", "descriptor", "godot_source", "conversion_report"}
    assert version.licence["status"] == "review" and version.sources["integration"]["actor"] == "integration:godot"
    raw = env.c.get(_ver_url(env, done, "/descriptor"))
    assert hashlib.sha256(raw.content).hexdigest() == done["descriptor_sha256"] == version.artifacts["descriptor"][
        "sha256"]
    d = parse_descriptor(raw.content)
    assert d.asset_ref.asset_id == ref["asset_id"] and d.source_provenance["preview_id"] == receipt["preview_id"]
    assert [s.slot_id for s in d.material_slots] == ["bark", "foliage"]
    assert all({"portable_glb_v1", "godot_static_source_v1"} == set(s.surfaces) for s in d.material_slots)
    assert d.licence == {"status": "review", "declared": "CC0-1.0", "credit": "me", "source_uri": None}
    detail = env.c.get(_ver_url(env, done)).json()
    assert detail["descriptor"]["state"] == "ready" and detail["source_available"] is True
    assert {x["representation"] for x in detail["deliveries"]} == {"portable_glb_v1", "godot_static_source_v1"}
    entry = env.c.post(env.url("/resolve"), json={"refs": [ref], "target": {"representations": [
        "portable_glb_v1", "godot_static_source_v1"]}}).json()["entries"][0]
    assert entry["state"] == "ready" and len(entry["deliveries"]) == 2
    src = next(x for x in entry["deliveries"] if x["representation"] == "godot_static_source_v1")
    manifest = env.c.get(env.url(f"/deliveries/{src['delivery_id']}/manifest")).json()
    (file,) = manifest["files"]
    body = env.c.get(env.url(f"/artifacts/{file['artifact_id']}/content"))
    assert file["path"] == "source.zip" and body.content == parts["source"]
    assert env.c.get(env.url(f"/assets/{ref['asset_id']}/versions/{ref['version_id']}/thumbnail")).status_code == 200
    events = env.c.get("/api/integration/v1/changes", params={"cursor": cursor, "timeout_s": 0}).json()["events"]
    assert {"type": "asset_current_changed", "library_id": env.lib, "asset_id": ref["asset_id"]} in events
    assert env.previews_on_disk() == []
    assert env.c.get(env.url("/publication-operations/publish-tree-1")).json() == {
        "idempotency_key": "publish-tree-1", "op_id": done["operation"]["op_id"], "state": "committed",
        "asset_id": ref["asset_id"], "version_id": ref["version_id"], "display_version": 1}
    unknown = env.c.get(env.url("/publication-operations/never-used-key")).json()
    assert unknown["state"] == "unknown"


def test_retry_same_key_is_idempotent_and_other_payload_conflicts(env: PubEnv) -> None:
    receipt = env.preview(source_parts("primitive_prop"))
    first = env.commit(receipt, "idem-key-001")
    assert env.commit(receipt, "idem-key-001") == first
    r = env.c.post(env.url("/publications:commit"), json=commit_body(receipt, "idem-key-001", name="Other"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_conflict"
    versions = env.c.get(env.url(f"/assets/{first['asset_ref']['asset_id']}")).json()["versions"]
    assert len(versions) == 1


def test_new_version_stale_pointer_and_target_rules(env: PubEnv) -> None:
    first = env.commit(env.preview(source_parts("primitive_prop")), "ver-key-0001")
    aid, v1 = first["asset_ref"]["asset_id"], first["asset_ref"]["version_id"]
    second = env.commit(env.preview(source_parts("primitive_prop_v2")), "ver-key-0002", target_asset_id=aid,
                        expected_current_version=v1)
    assert second["asset_ref"]["asset_id"] == aid and second["display_version"] == 2
    v2 = second["asset_ref"]["version_id"]
    assert env.c.get(env.url(f"/assets/{aid}")).json()["current_version_id"] == v2
    prev3 = env.preview(source_parts("primitive_prop"))
    stale = env.c.post(env.url("/publications:commit"), json=commit_body(
        prev3, "ver-key-0003", target_asset_id=aid, expected_current_version=v1))
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_pointer"
    assert stale.json()["error"]["details"] == {"current_version_id": v2}
    assert env.preview(source_parts("primitive_prop"))["package_sha256"]  # source bytes are still previewable
    for over in ({"target_asset_id": aid}, {"expected_current_version": v2}):
        r = env.c.post(env.url("/publications:commit"), json=commit_body(prev3, "ver-key-0004", **over))
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
    missing = env.c.post(env.url("/publications:commit"), json=commit_body(
        prev3, "ver-key-0005", target_asset_id="ast_0000000000000000", expected_current_version=v2))
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "asset_not_found"


def test_expired_preview_and_sweep(env: PubEnv) -> None:
    receipt = env.preview(glb_parts())
    staging = env.api.studio.settings.instance_dir / "staging/integration" / receipt["preview_id"]
    stored = json.loads((staging / "receipt.json").read_text())
    stored["expires_at"] = "2000-01-01T00:00:00.000Z"
    (staging / "receipt.json").write_text(json.dumps(stored))
    r = env.c.post(env.url("/publications:commit"), json=commit_body(receipt, "expired-key-1"))
    assert r.status_code == 410 and r.json()["error"]["code"] == "preview_expired"
    assert sp.sweep_expired(env.api.studio.settings) == 1 and not staging.exists()


def test_commit_checks_hashes_owner_and_staged_bytes(env: PubEnv) -> None:
    receipt = env.preview(glb_parts())
    r = env.c.post(env.url("/publications:commit"), json=commit_body(receipt, "hash-key-001", portable_sha256="1" * 64))
    assert r.status_code == 409 and r.json()["error"]["code"] == "integrity_mismatch"
    other = client(env.app, make_token(env.app, "other", ["assets:publish"], [env.lib]))
    r = other.post(env.url("/publications:commit"), json=commit_body(receipt, "hash-key-002"))
    assert r.status_code == 403
    staging = env.api.studio.settings.instance_dir / "staging/integration" / receipt["preview_id"] / "parts"
    (staging / "portable").write_bytes(glb(2))
    r = env.c.post(env.url("/publications:commit"), json=commit_body(receipt, "hash-key-003"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "integrity_mismatch"
    assert env.c.get(env.url("/assets")).json()["items"] == []


def test_glb_only_publication(env: PubEnv) -> None:
    done = env.commit(env.preview(glb_parts(2)), "glb-only-key-1")
    assert set(_version(env, done).artifacts) == {"model", "preview", "descriptor"}
    assert [d["representation"] for d in done["deliveries"]] == ["portable_glb_v1"]
    entry = env.c.post(env.url("/resolve"), json={"refs": [done["asset_ref"]], "target": {
        "representations": ["godot_static_source_v1"]}}).json()["entries"][0]
    assert entry["state"] == "unsupported"
    assert env.c.get(_ver_url(env, done)).json()["source_available"] is False


def test_source_dependency_resolution(env: PubEnv) -> None:
    other_lib = new_project(env.api, "Deps")
    both = client(env.app, make_token(env.app, "both", ["assets:read", "assets:publish"], [env.lib, other_lib]))
    dep_in_a = env.commit(env.preview(glb_parts()), "dep-key-0001")
    dep_in_b = env.preview(glb_parts(), c=both, lib=other_lib)
    r = both.post(env.url("/publications:commit", other_lib), json=commit_body(dep_in_b, "dep-key-0002"))
    assert r.status_code == 200
    dep_b = r.json()
    cluster_parts = source_parts("prop_cluster")  # descriptor draft + report only; the zip is rebuilt per case

    def parts_with(dep: dict[str, Any], sha: str, delivery: str | None) -> dict[str, bytes]:
        return {**cluster_parts, "source": cluster_zip(dep["asset_ref"], sha, delivery, env.server_id)}

    good = parts_with(dep_in_a, dep_in_a["descriptor_sha256"], dep_in_a["deliveries"][0]["delivery_id"])
    receipt = env.preview(good)
    assert receipt["source"]["asset_dependencies"]
    cluster = env.commit(receipt, "cluster-key-01")
    source = next(d for d in cluster["deliveries"] if d["representation"] == "godot_static_source_v1")
    deps = env.c.get(env.url(f"/deliveries/{source['delivery_id']}/manifest")).json()["dependencies"]
    portable_dep = dep_in_a["deliveries"][0]
    assert [(d["asset_ref"], d["delivery_id"], d["manifest_sha256"]) for d in deps] == [
        (dep_in_a["asset_ref"], portable_dep["delivery_id"], portable_dep["manifest_sha256"])]
    for bad in (parts_with(dep_in_a, "0" * 64, None),  # hash mismatch
                parts_with(dep_in_a, dep_in_a["descriptor_sha256"], "dlv_0000000000000000"),  # unknown delivery
                parts_with(dep_b, dep_b["descriptor_sha256"], None)):  # library not granted to the token
        r = env.c.post(env.url("/publications:preview"), files=files_of(bad))
        assert r.status_code == 422 and r.json()["error"]["code"] == "unsupported_source_dependency", r.text
    assert both.post(env.url("/publications:preview"), files=files_of(parts_with(dep_b, dep_b["descriptor_sha256"],
                                                                                    None))).status_code == 200


def test_read_token_gets_forbidden_before_body_validation(env: PubEnv) -> None:
    reader = client(env.app, make_token(env.app, "reader-only", ["assets:read"], [env.lib]))
    r = reader.post(env.url("/publications:commit"), json={})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"

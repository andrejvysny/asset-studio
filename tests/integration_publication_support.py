"""Helpers for publication tests: real GLBs, drafts derived from source manifests, multipart uploads."""
from __future__ import annotations

import io
import json
import struct
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import trimesh
from assetstudio_core.canonical_v1 import canonical_bytes
from assetstudio_core.source_manifest import MANIFEST_NAME, SourcePackageManifestV1, parse_source_manifest
from fastapi.testclient import TestClient

from tests.conftest import Api, new_project
from tests.integration_support import client, integration_app_for, make_token

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "contracts/godot-integration/v1/fixtures"
V1 = "/api/integration/v1"
sys.path.insert(0, str(ROOT / "scripts"))


def glb(slots: int = 1) -> bytes:
    """Static GLB with one mesh (one primitive) per slot; distinct sizes so geometries never merge."""
    scene = trimesh.Scene()
    for i in range(slots):
        box = trimesh.creation.box(extents=(1 + 0.1 * i, 1 + 0.05 * i, 1 + 0.2 * i))
        scene.add_geometry(box, geom_name=f"m{i}", node_name=f"n{i}")
    buf = io.BytesIO()
    scene.export(buf, file_type="glb")
    return buf.getvalue()


def patched_glb(slots: int = 1, **top_level: Any) -> bytes:
    """A GLB whose JSON chunk gains extra top-level keys (e.g. skins, animations)."""
    data = glb(slots)
    length = struct.unpack_from("<I", data, 12)[0]
    doc = json.loads(data[20:20 + length])
    doc.update(top_level)
    raw = json.dumps(doc, separators=(",", ":")).encode()
    raw += b" " * (-len(raw) % 4)
    rest = data[20 + length:]
    body = struct.pack("<I4s", len(raw), b"JSON") + raw + rest
    return struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body


def fixture_zip(name: str) -> bytes:
    return (FIXTURES / "source_packages/valid" / f"{name}.zip").read_bytes()


def manifest_of(zip_bytes: bytes) -> SourcePackageManifestV1:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return parse_source_manifest(zf.read(MANIFEST_NAME))


def draft_for(manifest: SourcePackageManifestV1 | None, slots: int = 1) -> dict[str, Any]:
    """Draft whose placement equals the manifest's (portable surface i = mesh i, primitive 0)."""
    if manifest is None:
        placement: dict[str, Any] = {
            "placement_anchor": ["0", "0", "0"], "footprint_radius_m": "1", "scale_range": ["0.5", "2"],
            "height_offset_range_m": ["-0.1", "0.5"], "default_grounding": "FOLLOW_TERRAIN",
            "material_slots": [{"slot_id": f"slot{i}", "role": "surface", "source_surfaces": []}
                               for i in range(slots)]}
    else:
        placement = manifest.placement.model_dump(mode="json")
    out_slots = []
    for i, s in enumerate(placement["material_slots"]):
        surfaces: dict[str, Any] = {"portable_glb_v1": [{"mesh": i, "primitive": 0}]}
        if manifest is not None:
            surfaces["godot_static_source_v1"] = s["source_surfaces"]
        out_slots.append({"slot_id": s["slot_id"], "role": s["role"], "surfaces": surfaces})
    collision = ({"source": "godot_static_source_v1", "shape_count": 1, "shape_types": ["box"]}
                 if manifest is not None and "static_collision" in manifest.capabilities else None)
    return {"schema_version": 1, **{k: placement[k] for k in (
        "placement_anchor", "footprint_radius_m", "scale_range", "height_offset_range_m", "default_grounding")},
        "material_slots": out_slots, "collision": collision, "preview_warnings": []}


def source_parts(name: str, **override: Any) -> dict[str, bytes]:
    """portable + descriptor + source + report for a valid fixture package."""
    zdata = override.pop("zip", None) or fixture_zip(name)
    manifest = manifest_of(zdata)
    n = len(manifest.placement.material_slots)
    return {"portable": glb(n), "descriptor": canonical_bytes(draft_for(manifest)), "source": zdata,
            "report": canonical_bytes(manifest.conversion_report.model_dump(mode="json")), **override}


def glb_parts(slots: int = 1) -> dict[str, bytes]:
    return {"portable": glb(slots), "descriptor": canonical_bytes(draft_for(None, slots))}


def files_of(parts: dict[str, bytes]) -> dict[str, tuple[str, bytes]]:
    names = {"source": "source.zip", "portable": "portable.glb", "descriptor": "descriptor.json",
             "thumbnail": "thumbnail.png", "report": "report.json"}
    return {k: (names.get(k, k), v) for k, v in parts.items()}


def commit_body(receipt: dict[str, Any], key: str, **over: Any) -> dict[str, Any]:
    return {"preview_id": receipt["preview_id"], "package_sha256": receipt["package_sha256"],
            "portable_sha256": receipt["portable_sha256"],
            "descriptor_draft_sha256": receipt["descriptor_draft_sha256"], "name": "Rock", "idempotency_key": key,
            **over}


@dataclass
class PubEnv:
    api: Api
    lib: str
    c: TestClient
    app: Any
    token: str
    server_id: str

    def url(self, path: str, lib: str | None = None) -> str:
        return f"{V1}/libraries/{lib or self.lib}{path}"

    def preview(self, parts: dict[str, bytes], expect: int = 200, c: TestClient | None = None,
                lib: str | None = None) -> dict[str, Any]:
        r = (c or self.c).post(self.url("/publications:preview", lib), files=files_of(parts))
        assert r.status_code == expect, r.text
        return r.json()

    def commit(self, receipt: dict[str, Any], key: str, expect: int = 200, **over: Any) -> dict[str, Any]:
        r = self.c.post(self.url("/publications:commit"), json=commit_body(receipt, key, **over))
        assert r.status_code == expect, r.text
        return r.json()

    def ctx(self, lib: str | None = None) -> Any:
        return self.api.studio.registry.get(lib or self.lib)

    def tree(self, *tops: str) -> dict[str, int]:
        root = self.ctx().root
        return {str(p.relative_to(root)): p.stat().st_size for top in tops if (root / top).exists()
                for p in sorted((root / top).rglob("*")) if p.is_file()}

    def previews_on_disk(self) -> list[str]:
        root = self.api.studio.settings.instance_dir / "staging" / "integration"
        return sorted(p.name for p in root.iterdir()) if root.exists() else []


def make_env(make_api: Any) -> PubEnv:
    api = make_api(engine="none", coordinator=False)
    lib = new_project(api)
    app, fastapi_app = integration_app_for(api)
    token = make_token(app, "godot", ["assets:read", "assets:publish"], [lib])
    return PubEnv(api, lib, client(app, token), app, token, fastapi_app.state.identity.server_id)


def external_uri_glb() -> bytes:
    from integration_fixture_sources import glb_with_external_uri

    return glb_with_external_uri()


def cluster_zip(dep_ref: dict[str, Any], descriptor_sha: str, delivery_id: str | None, server_id: str) -> bytes:
    """prop_cluster source package whose single dependency is the given (really published) asset version."""
    from assetstudio_core.canonical_v1 import asset_key
    from assetstudio_core.delivery import AssetDescriptorV1
    from assetstudio_core.source_manifest import source_manifest_bytes
    from integration_fixture_packages import BASE, EXACT, build_package_doc, zip_of
    from integration_fixture_sources import cluster_files
    from make_integration_fixtures import descriptor_docs

    ref = {**dep_ref, "server_id": server_id}
    key = asset_key(ref["server_id"], ref["library_id"], ref["asset_id"], ref["version_id"])
    deps = {key: {"asset_ref": ref, "descriptor_sha256": descriptor_sha, "representation": "portable_glb_v1",
                  "delivery_id": delivery_id}}
    files = cluster_files()
    desc = AssetDescriptorV1.model_validate(descriptor_docs()["prop_cluster"])
    doc = build_package_doc(files, "scenes/cluster.tscn", desc, BASE, EXACT, deps,
                            {"res://deps/primitive_prop.glb": (key, "portable.glb")})
    manifest = SourcePackageManifestV1.model_validate(doc)
    return zip_of(source_manifest_bytes(manifest), files)

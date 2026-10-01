"""Regenerate the Godot integration v1 fixture bundle (descriptors, manifests, locks, source packages, INDEX.json).

Usage: uv run python scripts/make_integration_fixtures.py [--check]
--check exits non-zero when committed fixtures differ from generated output, or when stray files exist (used by tests).
Output is deterministic: fixed ids, fixed timestamps, sorted members, canonical JSON.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import integration_fixture_sources as src  # noqa: E402
from assetstudio_core.canonical import pretty_json  # noqa: E402
from assetstudio_core.canonical_v1 import decimal_str  # noqa: E402
from assetstudio_core.delivery import (  # noqa: E402
    AssetDescriptorV1,
    DeliveryManifestV1,
    descriptor_bytes,
    manifest_bytes,
)
from assetstudio_core.ids import derived_id  # noqa: E402
from assetstudio_core.project_lock import ProjectAssetLockV1, lock_bytes  # noqa: E402
from assetstudio_core.publication_draft import DescriptorDraftV1, draft_bytes  # noqa: E402
from assetstudio_core.source_manifest import SourcePackageManifestV1  # noqa: E402
from integration_fixture_packages import (  # noqa: E402
    CLUSTER,
    CRYSTAL,
    HUT,
    PROP,
    PROP2,
    PROP_B,
    ROCK,
    TREE,
    Json,
    hostile_packages,
    key_of,
    ref,
    sha,
    valid_packages,
)

FIXTURES = Path(__file__).resolve().parents[1] / "contracts/godot-integration/v1/fixtures"
SCHEMA_INVALID, SEMANTIC_INVALID = "schema_invalid", "semantic_invalid"


class Bundle:
    """Collects fixture files and their INDEX entries."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.index: list[Json] = []

    def add(self, path: str, data: bytes, kind: str, expected: str, description: str,
            detail: str | None = None) -> None:
        self.files[path] = data
        entry: Json = {"path": path, "sha256": sha(data), "kind": kind, "expected": expected,
                       "description": description}
        if detail:
            entry["detail"] = detail
        self.index.append(entry)


# ---------------------------------------------------------------- descriptors

def psurf(mesh: int, primitive: int) -> Json:
    return {"mesh": mesh, "primitive": primitive}


def slot(slot_id: str, role: str, portable: list[Json], source: list[Json]) -> Json:
    return {"slot_id": slot_id, "role": role,
            "surfaces": {"portable_glb_v1": portable, "godot_static_source_v1": source}}


def descriptor(spec: tuple[str, str, str], bounds: tuple[list[str], list[str]], anchor: list[str], footprint: str,
               slots: list[Json], *, collision: Json | None = None, warnings: list[str] | None = None,
               scale: tuple[str, str] = ("0.5", "2"), height: tuple[str, str] = ("-0.1", "0.5"),
               note: str = "") -> Json:
    return {
        "schema_version": 1, "asset_ref": ref(spec), "kind": "model3d", "units": "m", "up_axis": "+Y",
        "forward_axis": "+Z", "bounds_min": bounds[0], "bounds_max": bounds[1], "placement_anchor": anchor,
        "footprint_radius_m": footprint, "scale_range": list(scale), "height_offset_range_m": list(height),
        "default_grounding": "FOLLOW_TERRAIN", "material_slots": slots, "collision": collision,
        "preview_warnings": warnings or [],
        "source_provenance": {"generator": "assetstudio-integration-fixtures", "method": "procedural", "note": note},
        "licence": {"name": "CC0-1.0", "rights_verified": False}}


def rock_bounds() -> tuple[list[str], list[str]]:
    lo, hi = src.rock_mesh().bounds
    return [decimal_str(float(v), "floor") for v in lo], [decimal_str(float(v), "ceil") for v in hi]


def descriptor_docs() -> dict[str, Json]:
    one = [slot("body", "surface", [psurf(0, 0)], [{"node_path": "Body", "surface": 0}])]
    return {
        "primitive_prop": descriptor(PROP, (["-0.5", "0", "-1"], ["0.5", "0.5", "1"]), ["0.25", "0", "-0.5"], "1.1",
                                     one),
        "primitive_prop_v2": descriptor(
            PROP2, (["-0.5", "0", "-1"], ["0.5", "0.5", "1"]), ["0", "0", "0.5"], "1.1",
            [slot("body", "surface", [psurf(0, 0)], [{"node_path": "Crate", "surface": 0}]),
             slot("lid", "surface", [psurf(1, 0)], [{"node_path": "Lid", "surface": 0}])],
            note="version 2: anchor moved, second material slot, nodes renamed"),
        "primitive_prop_library_b": descriptor(PROP_B, (["-0.5", "0", "-1"], ["0.5", "0.5", "1"]),
                                               ["0.25", "0", "-0.5"], "1.1", one, note="same ids, other library"),
        "textured_tree": descriptor(
            TREE, (["-1", "0", "-1"], ["1", "4", "1"]), ["0", "0", "0"], "1.5",
            [slot("bark", "bark", [psurf(0, 0)], [{"node_path": "Trunk", "surface": 0}]),
             slot("foliage", "foliage", [psurf(0, 1)], [{"node_path": "Crown", "surface": 0}])],
            warnings=["alpha_mask_edges_may_differ"], scale=("0.6", "1.8"), height=("-0.5", "0.5")),
        "vertex_color_rock_with_collision": descriptor(
            ROCK, rock_bounds(), ["0", "0", "0"], "0.8",
            [slot("surface", "stone", [psurf(0, 0)], [{"node_path": "Mesh", "surface": 0}])],
            collision={"source": "godot_static_source_v1", "shape_count": 1, "shape_types": ["box"]}),
        "csg_hut": descriptor(
            HUT, (["-2.2", "0", "-2.2"], ["2.2", "2.75", "2.2"]), ["0", "0", "0"], "2.3",
            [slot("walls", "surface", [psurf(0, 0)], [{"node_path": "Shell/Walls", "surface": 0}]),
             slot("roof", "surface", [psurf(0, 1)], [{"node_path": "Shell/Roof", "surface": 0}])],
            scale=("0.5", "1.5")),
        "custom_shader_crystal": descriptor(
            CRYSTAL, (["-0.5", "0", "-0.5"], ["0.5", "2", "0.5"]), ["0", "0", "0"], "0.6",
            [slot("crystal", "emissive", [psurf(0, 0)], [{"node_path": "Gem", "surface": 0}])],
            warnings=["custom_shader_approximated"]),
        "prop_cluster": descriptor(
            CLUSTER, (["-2", "0", "-1"], ["2", "0.5", "1"]), ["0", "0", "0"], "2.3",
            [slot("base", "surface", [psurf(0, 0)], [{"node_path": "Base", "surface": 0}])], scale=("0.5", "1")),
    }


def validated_descriptors(docs: dict[str, Json]) -> dict[str, tuple[AssetDescriptorV1, bytes]]:
    out = {}
    for name, doc in docs.items():
        model = AssetDescriptorV1.model_validate(doc)
        out[name] = (model, descriptor_bytes(model))
    return out


def invalid_descriptors(base: Json) -> list[tuple[str, Json | bytes, str, str]]:
    def edit(**kw: Any) -> Json:
        return {**base, **kw}

    float_doc = pretty_json(edit(footprint_radius_m="__F__")).replace(b'"__F__"', b"1.1")
    return [
        ("float_literal", float_doc, SCHEMA_INVALID, "footprint_radius_m is the JSON number 1.1, not a decimal string"),
        ("non_canonical_decimal", edit(footprint_radius_m="1.10"), SCHEMA_INVALID, "trailing zero in decimal string"),
        ("negative_zero", edit(placement_anchor=["-0", "0", "0"]), SCHEMA_INVALID, '"-0" must be written as "0"'),
        ("bounds_inverted", edit(bounds_min=["0.5", "0", "-1"], bounds_max=["-0.5", "0.5", "1"]), SEMANTIC_INVALID,
         "bounds_min.x > bounds_max.x"),
        ("zero_extent_all_axes", edit(bounds_min=["0", "0", "0"], bounds_max=["0", "0", "0"]), SEMANTIC_INVALID,
         "no positive extent on any axis"),
        ("unknown_field", edit(display_name="Prop"), SCHEMA_INVALID, "additional property"),
        ("forward_axis_minus_z", edit(forward_axis="-Z"), SCHEMA_INVALID, "forward_axis must be +Z (docs/adr/0001)"),
        ("duplicate_slot_id", edit(material_slots=base["material_slots"] * 2), SEMANTIC_INVALID,
         "slot_id repeated"),
        ("scale_min_zero", edit(scale_range=["0", "2"]), SCHEMA_INVALID, "scale_range min must be > 0"),
    ]


DRAFT_KEYS = ("placement_anchor", "footprint_radius_m", "scale_range", "height_offset_range_m", "default_grounding",
              "material_slots", "collision", "preview_warnings")


def draft_docs(base: Json) -> tuple[Json, list[tuple[str, Json, str, str]]]:
    """Descriptor draft = the publisher-declared placement fields of a descriptor (server computes the rest)."""
    draft: Json = {"schema_version": 1, **{k: base[k] for k in DRAFT_KEYS}}
    no_portable = [{**draft["material_slots"][0], "surfaces": {
        "godot_static_source_v1": draft["material_slots"][0]["surfaces"]["godot_static_source_v1"]}}]
    invalid = [
        ("missing_portable_surfaces", {**draft, "material_slots": no_portable}, SCHEMA_INVALID,
         "slot has no portable_glb_v1 surfaces"),
        ("duplicate_slot_id", {**draft, "material_slots": draft["material_slots"] * 2}, SEMANTIC_INVALID,
         "slot_id repeated")]
    return draft, invalid


# ---------------------------------------------------------------- delivery manifests

def delivery_doc(spec: tuple[str, str, str], dlv: str, desc_sha: str, rep: str, files: list[Json], entry: str,
                 caps: list[str], deps: list[Json] | None = None, profile: str = "portable-default") -> Json:
    return {
        "schema_version": 1, "delivery_id": f"dlv_00000000000000{dlv}", "asset_ref": ref(spec),
        "descriptor_sha256": desc_sha, "representation": rep, "profile_id": profile, "profile_version": "1.0.0",
        "preparer": {"name": "assetstudio-fixtures", "version": "1.0.0"}, "entrypoint": entry, "files": files,
        "dependencies": deps or [], "required_capabilities": caps}


def file_entry(path: str, data: bytes, media: str) -> Json:
    return {"path": path, "sha256": sha(data), "size": len(data), "media_type": media,
            "artifact_id": derived_id("art", "fixture", path, sha(data))}


def invalid_manifests(base: Json) -> list[tuple[str, Json, str, str]]:
    f0 = base["files"][0]

    def edit(**kw: Any) -> Json:
        return {**base, **kw}

    return [
        ("url_field", edit(url="https://example.com/portable.glb"), SCHEMA_INVALID, "manifests carry no fetch URL"),
        ("absolute_path", edit(entrypoint="/etc/portable.glb", files=[{**f0, "path": "/etc/portable.glb"}]),
         SCHEMA_INVALID, "absolute file path"),
        ("parent_path", edit(entrypoint="../portable.glb", files=[{**f0, "path": "../portable.glb"}]), SCHEMA_INVALID,
         "'..' segment"),
        ("casefold_duplicate_paths", edit(files=[f0, {**f0, "path": "PORTABLE.GLB"}]), SEMANTIC_INVALID,
         "paths differ only by case"),
        ("entrypoint_not_in_files", edit(entrypoint="other.glb"), SEMANTIC_INVALID, "entrypoint is not a files[].path"),
        ("unknown_capability", edit(required_capabilities=["teleport_v9"]), SCHEMA_INVALID,
         "capability not in capabilities.json"),
    ]


# ---------------------------------------------------------------- locks

def lock_dep(r: Json, desc_sha: str, deliveries: dict[str, Json], requires: list[str] | None = None) -> Json:
    return {"asset_ref": r, "descriptor_sha256": desc_sha, "deliveries": deliveries, "requires": requires or []}


def lock_delivery(dlv: str, manifest_sha: str, profile: str = "portable-default") -> Json:
    return {"delivery_id": f"dlv_00000000000000{dlv}", "manifest_sha256": manifest_sha, "profile_id": profile,
            "profile_version": "1.0.0"}


def preserve() -> Json:
    return {"mode": "preserve", "profile_id": None, "profile_sha256": None}


def lock_docs(k: dict[str, Any]) -> tuple[Json, Json, list[tuple[str, Json, str, str]]]:
    k1, k2 = key_of(ref(PROP)), key_of(ref(PROP2))
    gen = {"addon_version": "1.0.0", "installer_version": "1.0.0"}
    two = {
        "schema_version": 1, "generator": gen,
        "dependencies": {
            k1: lock_dep(ref(PROP), k["d_prop"], {"portable_glb_v1": lock_delivery("d1", k["m_prop"])}),
            k2: lock_dep(ref(PROP2), k["d_prop2"], {"portable_glb_v1": lock_delivery("d2", k["m_prop2"])})},
        "bindings": {
            "prop-a": {"asset_key": k1, "representation": "portable_glb_v1", "material_policy": preserve(),
                       "update_policy": "prompt"},
            "prop-b": {"asset_key": k2, "representation": "portable_glb_v1", "update_policy": "prompt",
                       "material_policy": {"mode": "project_mapping", "profile_id": "project-default",
                                           "profile_sha256": sha(b"project-default-profile")}}},
        "roots": [{"owner_kind": "scene_binding", "owner_id": "prop-a", "asset_keys": [k1]},
                  {"owner_kind": "scene_binding", "owner_id": "prop-b", "asset_keys": [k2]},
                  {"owner_kind": "world_generation", "owner_id": "world-1", "asset_keys": [k1, k2]}]}
    kc = key_of(ref(CLUSTER))
    cluster = {
        "schema_version": 1, "generator": gen,
        "dependencies": {
            k1: lock_dep(ref(PROP), k["d_prop"], {"portable_glb_v1": lock_delivery("d1", k["m_prop"])}),
            kc: lock_dep(ref(CLUSTER), k["d_cluster"], {"godot_static_source_v1": lock_delivery(
                "d4", k["m_cluster"], "godot-static-source")}, [k1])},
        "bindings": {"cluster-1": {"asset_key": kc, "representation": "godot_static_source_v1",
                                   "material_policy": {"mode": "override", "profile_id": None, "profile_sha256": None},
                                   "update_policy": "prompt"}},
        "roots": [{"owner_kind": "scene_binding", "owner_id": "cluster-1", "asset_keys": [kc, k1]}]}

    def deps(**patch: Any) -> Json:
        return {**two, "dependencies": {**two["dependencies"], **patch}}

    d1, d2 = two["dependencies"][k1], two["dependencies"][k2]
    invalid = [
        ("cycle", deps(**{k1: {**d1, "requires": [k2]}, k2: {**d2, "requires": [k1]}}), SEMANTIC_INVALID,
         "two dependencies require each other"),
        ("missing_closure", deps(**{k1: {**d1, "requires": ["f" * 64]}}), SEMANTIC_INVALID,
         "requires a key that is not in the lock"),
        ("key_mismatch", {**two, "dependencies": {"e" * 64: d1, k2: d2}, "bindings": {}, "roots": []},
         SEMANTIC_INVALID, "dependency key differs from the key recomputed from asset_ref"),
        ("update_policy_auto", {**two, "bindings": {"prop-a": {**two["bindings"]["prop-a"], "update_policy": "auto"}}},
         SCHEMA_INVALID, "update_policy must be 'prompt'"),
    ]
    return two, cluster, invalid


# ---------------------------------------------------------------- assembly

def add_json_cases(b: Bundle, folder: str, kind: str, cases: list[tuple[str, Any, str, str]]) -> None:
    for name, doc, expected, why in cases:
        data = doc if isinstance(doc, bytes) else pretty_json(doc)
        b.add(f"{folder}/invalid/{name}.json", data, kind, expected, why)


def build_all() -> dict[str, bytes]:
    b = Bundle()
    descs = validated_descriptors(descriptor_docs())
    for name, (_, raw) in descs.items():
        b.add(f"descriptors/valid/{name}.json", raw, "descriptor", "schema_valid", f"valid descriptor: {name}")
    add_json_cases(b, "descriptors", "descriptor", invalid_descriptors(descriptor_docs()["primitive_prop"]))

    draft, invalid_drafts = draft_docs(descriptor_docs()["primitive_prop"])
    b.add("descriptor_drafts/valid/primitive_prop.json", draft_bytes(DescriptorDraftV1.model_validate(draft)),
          "descriptor_draft", "schema_valid", "valid descriptor draft: primitive_prop")
    add_json_cases(b, "descriptor_drafts", "descriptor_draft", invalid_drafts)

    glbs = {"primitive_prop": src.prop_glb(), "primitive_prop_v2": src.prop_v2_glb()}
    for name, data in glbs.items():
        b.add(f"glb/{name}.portable.glb", data, "glb", "valid", f"portable GLB of the {name} delivery manifest")
    manifests: dict[str, tuple[Json, bytes]] = {}
    for name, spec, dlv in (("primitive_prop", PROP, "d1"), ("primitive_prop_v2", PROP2, "d2")):
        doc = delivery_doc(spec, dlv, sha(descs[name][1]), "portable_glb_v1",
                           [file_entry("portable.glb", glbs[name], "model/gltf-binary")], "portable.glb", [])
        raw = manifest_bytes(DeliveryManifestV1.model_validate(doc))
        manifests[name] = (doc, raw)
        b.add(f"manifests/valid/portable_{name}.json", raw, "manifest", "schema_valid", f"portable delivery of {name}")

    packages = valid_packages(descs)
    for name, (zdata, _model) in packages.items():
        b.add(f"source_packages/valid/{name}.zip", zdata, "source_package", "valid",
              f"valid GodotStaticSourcePackageV1: {name}")
    for name, spec, dlv, deps in (("textured_tree", TREE, "d3", None), ("prop_cluster", CLUSTER, "d4", True)):
        dependencies = [{"asset_key": key_of(ref(PROP)), "asset_ref": ref(PROP),
                         "descriptor_sha256": sha(descs["primitive_prop"][1]), "representation": "portable_glb_v1",
                         "delivery_id": "dlv_00000000000000d1",
                         "manifest_sha256": sha(manifests["primitive_prop"][1])}] if deps else []
        model = packages[name][1]
        caps = list(model.capabilities)
        doc = delivery_doc(spec, dlv, sha(descs[name][1]), "godot_static_source_v1",
                           [file_entry("source.zip", packages[name][0], "application/zip")], "source.zip", caps,
                           dependencies, "godot-static-source")
        raw = manifest_bytes(DeliveryManifestV1.model_validate(doc))
        manifests[name] = (doc, raw)
        b.add(f"manifests/valid/source_{name}.json", raw, "manifest", "schema_valid",
              f"static source delivery of {name}" + (" with an exact portable dependency" if deps else ""))
    add_json_cases(b, "manifests", "manifest", invalid_manifests(manifests["primitive_prop"][0]))
    add_source_manifest_cases(b, packages["prop_cluster"][1])

    for name, zdata, code, detail, why in hostile_packages(descs):
        b.add(f"source_packages/hostile/{name}.zip", zdata, "source_package", code, why, detail)

    two, cluster, invalid_locks = lock_docs({
        "d_prop": sha(descs["primitive_prop"][1]), "d_prop2": sha(descs["primitive_prop_v2"][1]),
        "d_cluster": sha(descs["prop_cluster"][1]), "m_prop": sha(manifests["primitive_prop"][1]),
        "m_prop2": sha(manifests["primitive_prop_v2"][1]), "m_cluster": sha(manifests["prop_cluster"][1])})
    for name, doc, why in (("two_versions", two, "two versions of one asset, two bindings, three roots"),
                           ("cluster_requires", cluster, "source delivery whose closure requires another dependency")):
        raw = lock_bytes(ProjectAssetLockV1.model_validate(doc))
        b.add(f"locks/valid/{name}.json", raw, "lock", "schema_valid", why)
    add_json_cases(b, "locks", "lock", invalid_locks)
    return finish(b)


def add_source_manifest_cases(b: Bundle, model: SourcePackageManifestV1) -> None:
    doc = model.model_dump(mode="json")
    key = next(iter(doc["asset_dependencies"]))
    bad_key = {**doc, "resource_map": {**doc["resource_map"], "assets/x.glb": {"kind": "package_file", "path": "a",
                                                                                "original_uid": None}}}
    no_dep = {**doc, "asset_dependencies": {}}
    cases = [("resource_map_key_not_res", bad_key, SCHEMA_INVALID, "resource_map key does not start with res://"),
             ("undeclared_asset_dependency", no_dep, SEMANTIC_INVALID,
              f"resource_map asset_dependency {key[:8]}.. missing from asset_dependencies")]
    add_json_cases(b, "source_manifests", "source_manifest", cases)


def finish(b: Bundle) -> dict[str, bytes]:
    vectors = FIXTURES / "vectors/canonical-v1.json"
    if vectors.exists():
        data = vectors.read_bytes()
        b.index.append({"path": "vectors/canonical-v1.json", "sha256": sha(data), "kind": "vector", "expected": "valid",
                        "description": "golden vectors: asset keys, decimals, canonical bytes"})
    vectors_json = FIXTURES / "vectors/canonical-json-v1.json"
    if vectors_json.exists():
        b.index.append({"path": "vectors/canonical-json-v1.json", "sha256": sha(vectors_json.read_bytes()),
                        "kind": "vector", "expected": "valid",
                        "description": "canonical JSON writer vectors (tagged input -> expected UTF-8 hex)"})
    b.index.sort(key=lambda e: e["path"])
    b.files["INDEX.json"] = pretty_json({"schema": "godot-integration/v1/fixture-index", "fixtures": b.index})
    return b.files


def on_disk() -> dict[str, bytes]:
    return {p.relative_to(FIXTURES).as_posix(): p.read_bytes()
            for p in sorted(FIXTURES.rglob("*")) if p.is_file() and not p.relative_to(FIXTURES).parts[0] == "vectors"}


def main() -> int:
    built = build_all()
    if "--check" in sys.argv:
        return 0 if built == on_disk() else 1
    for stale in set(on_disk()) - set(built):
        (FIXTURES / stale).unlink()
    for rel, data in built.items():
        path = FIXTURES / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    print(f"{len(built)} files under {FIXTURES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""A31: wrong-shaped glTF JSON is a controlled rejection in every GLB reader, never an AttributeError/TypeError."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from assetstudio_processing.glb import GlbRejected, inspect_container, validate_glb_bytes
from assetstudio_processing.render_materials import gltf_json
from assetstudio_processing.transforms import TransformRejected, inspect_static_glb

from tests.conftest import new_project
from tests.unit.test_transforms import build_glb


def _set(path: tuple[Any, ...], value: Any) -> Callable[[dict], None]:
    def mutate(doc: dict) -> None:
        target = doc
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return mutate


MALFORMED = [
    (("asset",), [1], "asset"),
    (("buffers",), 1, "buffers"),
    (("meshes",), 1, "meshes"),
    (("materials",), "x", "materials"),
    (("images",), {}, "images"),
    (("extensionsRequired",), 1, "extensionsRequired"),
    (("extensionsUsed",), [1], "extensionsUsed"),
    (("scene",), "0", "scene"),
    (("scene",), True, "scene"),
    (("meshes", 0), [], "meshes[0]"),
    (("meshes", 0, "primitives"), 1, "meshes[0].primitives"),
    (("meshes", 0, "primitives", 0), 5, "meshes[0].primitives[0]"),
    (("meshes", 0, "primitives", 0, "attributes"), [1], "meshes[0].primitives[0].attributes"),
    (("nodes", 0), "node", "nodes[0]"),
    (("nodes", 0, "children"), 1, "nodes[0].children"),
    (("nodes", 0, "mesh"), "0", "nodes[0].mesh"),
    (("nodes", 0, "translation"), "1,2,3", "nodes[0].translation"),
    (("scenes", 0, "nodes"), [0, "2"], "scenes[0].nodes"),
    (("accessors", 1), 7, "accessors[1]"),
    (("accessors", 1, "count"), "8", "accessors[1].count"),
    (("bufferViews", 2, "byteOffset"), None, "bufferViews[2].byteOffset"),
    (("materials", 0, "pbrMetallicRoughness"), [], "materials[0].pbrMetallicRoughness"),
    (("samplers", 0, "wrapS"), "repeat", "samplers[0].wrapS"),
]


@pytest.mark.parametrize("path,value,where", MALFORMED, ids=[m[2] + "=" + repr(m[1]) for m in MALFORMED])
def test_every_reader_rejects_wrong_shapes(path: tuple, value: Any, where: str) -> None:
    data = build_glb(mutate=_set(path, value))
    with pytest.raises(GlbRejected, match=where.replace("[", r"\[").replace("]", r"\]")):
        inspect_container(data)
    v = validate_glb_bytes(data)
    assert v["ok"] is False and v["checks"][0]["id"] == "container" and where in v["checks"][0]["detail"]
    with pytest.raises(TransformRejected) as e:
        inspect_static_glb(data)
    assert e.value.code == "corrupt_source"
    assert gltf_json(data) == {}


def test_well_formed_fixture_still_passes() -> None:
    data = build_glb()
    assert inspect_container(data)["meshes"] == 2
    assert inspect_static_glb(data)["vertex_count"] == 24
    assert gltf_json(data)["asset"]["generator"] == "test"


def test_import_preview_of_malformed_glb_is_a_validation_result(make_api) -> None:
    api = make_api(coordinator=False)
    pid = new_project(api)
    r = api.raw("POST", f"/api/v1/projects/{pid}/imports:preview",
                files={"file": ("bad.glb", build_glb(mutate=_set(("meshes",), 1)))})
    assert r.status_code < 500, r.text
    body = r.json()
    assert body["ok"] is False and "meshes must be an array" in body["validation"]["checks"][0]["detail"]

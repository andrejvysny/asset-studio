"""Geometry cleanup policy in the worker: call sequence on a recording mesh + param validation.
Run in the worker image (`make test-worker3d`); CPU only, no cumesh needed (remesh path not exercised)."""
from __future__ import annotations

import sys
import traceback
from typing import Any

import export
import main


class RecMesh:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str) -> Any:
        def rec(*a: Any, **k: Any) -> None:
            self.calls.append((name, a, k))
        return rec


def _run(**policy: str) -> list[tuple[str, tuple, dict]]:
    m = RecMesh()
    export._clean_and_simplify(m, 5000, False, None, None, None, None, None, **policy)  # type: ignore[arg-type]
    return m.calls


def _legacy() -> list[tuple[str, tuple, dict]]:
    h = {"max_hole_perimeter": export.HOLE_PERIMETER}
    loop = [("remove_duplicate_faces", (), {}), ("repair_non_manifold_edges", (), {}),
            ("remove_small_connected_components", (1e-5,), {}), ("fill_holes", (), h)]
    return ([("simplify", (15000,), {"verbose": False})] + loop + [("simplify", (5000,), {"verbose": False})] + loop
            + [("unify_face_orientations", (), {})])


def test_default_policy_matches_legacy_sequence() -> None:
    assert _run() == _legacy()
    assert _run(small_components="remove", fill_holes="upstream") == _legacy()


def test_preserve_skips_small_component_removal() -> None:
    names = [c[0] for c in _run(small_components="preserve")]
    assert "remove_small_connected_components" not in names and names.count("fill_holes") == 2


def test_disabled_skips_fill_holes() -> None:
    names = [c[0] for c in _run(fill_holes="disabled")]
    assert "fill_holes" not in names and names.count("remove_small_connected_components") == 2


def _export(**over: Any) -> dict[str, Any]:
    return {"exporter": "clean", "decimation_target": 5000, "texture_size": 1024, "remesh": False, **over}


def test_params_defaults_and_values() -> None:
    p = main._params("export", _export())
    assert p["small_components"] == "remove" and p["fill_holes"] == "upstream"
    p = main._params("export", _export(small_components="preserve", fill_holes="disabled"))
    assert p["small_components"] == "preserve" and p["fill_holes"] == "disabled"


def test_params_reject_bad_values_and_unknown_keys() -> None:
    for bad in (_export(small_components="keep"), _export(fill_holes="all"), _export(extra=1)):
        try:
            main._params("export", bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    try:
        main._params("generate", {"seed": 1, "pipeline_type": "512", "extra": 1})
    except ValueError:
        return
    raise AssertionError("generate accepted unknown key")


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}\n{traceback.format_exc()}")
    sys.exit(1 if failed else 0)

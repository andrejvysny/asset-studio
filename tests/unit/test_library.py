import copy
import importlib.util
import json
from pathlib import Path

import pytest
import yaml
from app.assignments import AssignmentStore, completed_attempts
from app.catalog import Catalog, CatalogError
from jobcore.job_io import Job, JobConflict

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("catalog_ids", ROOT / "scripts" / "catalog-ids.py")
catalog_ids = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(catalog_ids)  # type: ignore[union-attr]
slug, letters = catalog_ids.slug, catalog_ids.letters
CONV = yaml.safe_load((ROOT / "config" / "library.yaml").read_text())


@pytest.fixture(scope="module")
def cat() -> Catalog:
    return Catalog.load(ROOT / "config")


def test_slug_matches_design() -> None:
    assert slug("Containers & storage") == "containers_storage"
    assert slug("Lanterns & braziers") == "lanterns_braziers"
    assert slug("The Great Oak") == "great_oak"
    assert slug("Standing stones circle") == "standing_stones"


@pytest.mark.parametrize("slot", ["forest_anchor_lanterns_braziers_a", "forest_nature_rocks_boulders_b",
                                  "forest_prop_containers_storage_a", "forest_nature_undergrowth_floor_a",
                                  "forest_landmark_glowing_rune_a"])
def test_design_example_slots_exist(cat: Catalog, slot: str) -> None:
    assert slot in cat.slots


def test_slot_totals_match_catalog(cat: Catalog) -> None:
    assert len(cat.slots) == sum(f["count"] for b in cat.biomes for lyr in b["layers"] for f in lyr["families"]) == 1225


def test_letters_spreadsheet_style() -> None:
    assert [letters(k) for k in (0, 25, 26, 27, 51, 52)] == ["a", "z", "aa", "ab", "az", "ba"]


def _raw() -> dict:
    return json.loads((ROOT / "config" / "catalog.json").read_text())


def test_catalog_file_has_all_ids() -> None:
    data = _raw()
    assert catalog_ids.assign_ids(copy.deepcopy(data), CONV) == 0  # nothing left to assign


def test_v1_catalog_rejected() -> None:
    with pytest.raises(CatalogError):
        Catalog(_raw()["biomes"], CONV)


def test_duplicate_slot_id_rejected() -> None:
    data = _raw()
    fam = data["biomes"][0]["layers"][0]["families"][0]
    fam["slots"][1] = fam["slots"][0]
    with pytest.raises(CatalogError, match="duplicate"):
        Catalog(data, CONV)


def test_count_mismatch_rejected() -> None:
    data = _raw()
    data["biomes"][0]["layers"][0]["families"][0]["count"] += 1
    with pytest.raises(CatalogError, match="count"):
        Catalog(data, CONV)


def test_ids_stable_across_rename_and_growth() -> None:
    data = _raw()
    fam = data["biomes"][0]["layers"][4]["families"][0]
    old_id, old_slots = fam["id"], list(fam["slots"])
    fam["name"], fam["count"] = "Barrels, crates and chests", fam["count"] + 2
    assert catalog_ids.assign_ids(data, CONV) == 2
    assert fam["id"] == old_id and fam["slots"][: len(old_slots)] == old_slots
    assert fam["slots"][-2:] == [f"{old_id}_t", f"{old_id}_u"]


def test_coverage_shape(cat: Catalog) -> None:
    cov = cat.coverage({"forest_prop_containers_storage_a"})
    forest = next(r for r in cov["rows"] if r["id"] == "forest")
    assert len(cov["layers"]) == 7 and forest["cells"][4]["assigned"] == 1


def _completed_job(root: Path, validated: bool = True) -> tuple[Job, str]:
    job = Job.create(root, {"prompt": "barrel"})
    a = job.new_attempt({"index": 0})
    job.update_attempt(a["id"], state="completed", validated=validated, mesh={"triangles": 2000})
    return job, a["id"]


def test_assign_requires_validated_completed_attempt(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    store = AssignmentStore(tmp_path / "lib", out)
    job, aid = _completed_job(out, validated=False)
    with pytest.raises(JobConflict):
        store.assign("forest_prop_containers_storage_a", job.id, aid)
    job2, aid2 = _completed_job(out)
    entry = store.assign("forest_prop_containers_storage_a", job2.id, aid2)
    assert entry["triangles"] == 2000 and store.slot_of(job2.id, aid2) == "forest_prop_containers_storage_a"


def test_reassign_moves_attempt(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    store = AssignmentStore(tmp_path / "lib", out)
    job, aid = _completed_job(out)
    store.assign("forest_prop_containers_storage_a", job.id, aid)
    store.assign("forest_prop_containers_storage_b", job.id, aid)
    assert list(store.all()) == ["forest_prop_containers_storage_b"]
    assert store.unassign("forest_prop_containers_storage_b") and store.all() == {}


def test_completed_attempts_lists_only_validated(tmp_path: Path) -> None:
    _completed_job(tmp_path, validated=False)
    job, aid = _completed_job(tmp_path)
    items = completed_attempts([Job(tmp_path, d.name) for d in tmp_path.iterdir()])
    assert [(i["job_id"], i["attempt_id"]) for i in items] == [(job.id, aid)]

"""Asset catalog (config/catalog.json v2): explicit, stable layer/family/slot ids. Nothing is derived at runtime.

New ids are added only by scripts/catalog-ids.py (existing ids never change).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ID_RE = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)*$")


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class Slot:
    id: str
    biome: str
    layer: int
    family_id: str
    index: int


class Catalog:
    def __init__(self, data: dict, conventions: dict[str, Any]) -> None:
        if not isinstance(data, dict) or data.get("version") != 2:
            raise CatalogError("catalog.json must be v2 with explicit ids (run scripts/catalog-ids.py)")
        self.biomes: list[dict] = data["biomes"]
        self.conv = conventions
        self.slots: dict[str, Slot] = {}
        self.families: dict[str, dict] = {}
        self._validate_and_index()

    def _validate_and_index(self) -> None:
        seen: set[str] = set()

        def claim(i: str, where: str) -> None:
            if not isinstance(i, str) or not ID_RE.fullmatch(i):
                raise CatalogError(f"bad id {i!r} at {where}")
            if i in seen:
                raise CatalogError(f"duplicate id {i!r} at {where}")
            seen.add(i)

        for b in self.biomes:
            if b["id"] not in self.conv["biomes"]:
                raise CatalogError(f"biome {b['id']} missing from library.yaml")
            for li, layer in enumerate(b["layers"]):
                claim(layer.get("id"), f"{b['id']} layer {li}")
                for fam in layer["families"]:
                    claim(fam.get("id"), f"{b['id']}/{fam.get('name')}")
                    slots = fam.get("slots") or []
                    if len(slots) != fam["count"]:
                        raise CatalogError(f"{fam['id']}: count {fam['count']} != {len(slots)} slot ids")
                    self.families[fam["id"]] = {**fam, "biome": b["id"], "layer": li}
                    for k, sid in enumerate(slots):
                        claim(sid, fam["id"])
                        self.slots[sid] = Slot(sid, b["id"], li, fam["id"], k)

    @classmethod
    def load(cls, config_dir: Path) -> Catalog:
        return cls(json.loads((config_dir / "catalog.json").read_text()),
                   yaml.safe_load((config_dir / "library.yaml").read_text()))

    def slot_detail(self, slot_id: str) -> dict:
        s = self.slots[slot_id]
        fam = self.families[s.family_id]
        layer = self.conv["layers"][s.layer]
        role = self.conv["roles"][layer["role"]]
        return {
            "id": s.id, "biome": s.biome, "biome_name": self.conv["biomes"][s.biome]["name"],
            "layer": s.layer, "layer_name": layer["name"], "role": layer["role"], "family": fam["name"],
            "family_id": fam["id"], "variants": fam["variants"], "family_count": fam["count"], "index": s.index,
            "facts": {"triangle_budget": role["budget"], "texturing": role["texturing"], "route": role["route"],
                      "lods": role.get("lods", "—")},
        }

    def biome_tree(self, biome: str, assigned: dict[str, dict], layer: int | None = None) -> dict:
        b = next(x for x in self.biomes if x["id"] == biome)
        layers = []
        for li, lyr in enumerate(b["layers"]):
            if layer is not None and li != layer:
                continue
            fams = []
            for fam in lyr["families"]:
                slots = [{"id": sid, "assigned": sid in assigned,
                          **({"job_id": assigned[sid]["job_id"], "attempt_id": assigned[sid]["attempt_id"]}
                             if sid in assigned else {})} for sid in fam["slots"]]
                fams.append({"id": fam["id"], "name": fam["name"], "variants": fam["variants"], "count": fam["count"],
                             "assigned": sum(s["assigned"] for s in slots), "slots": slots})
            layers.append({"index": li, "id": lyr["id"], "title": lyr["title"], "name": self.conv["layers"][li]["name"],
                           "role": self.conv["layers"][li]["role"], "families": fams})
        meta = self.conv["biomes"][biome]
        return {"id": biome, "name": meta["name"], "prefix": meta["prefix"], "layers": layers}

    def _ids(self, biome: str, layer: int | None = None) -> list[str]:
        return [s.id for s in self.slots.values() if s.biome == biome and (layer is None or s.layer == layer)]

    def summary(self, assigned: set[str] | dict[str, Any]) -> list[dict]:
        out = []
        for b in self.biomes:
            meta = self.conv["biomes"][b["id"]]
            ids = self._ids(b["id"])
            out.append({"id": b["id"], "name": meta["name"], "prefix": meta["prefix"], "order": meta["order"],
                        "catalog_estimate": meta["catalog_estimate"], "base": len(ids),
                        "assigned": sum(i in assigned for i in ids),
                        "families": sum(len(lyr["families"]) for lyr in b["layers"]),
                        "layers": [{"index": li, "id": lyr["id"], "name": self.conv["layers"][li]["name"],
                                    "families": len(lyr["families"])} for li, lyr in enumerate(b["layers"])]})
        return out

    def coverage(self, assigned: set[str] | dict[str, Any]) -> dict:
        rows = []
        for b in self.summary(assigned):
            cells = []
            for li in range(len(self.conv["layers"])):
                ids = self._ids(b["id"], li)
                cells.append({"base": len(ids), "assigned": sum(i in assigned for i in ids)})
            rows.append({**b, "cells": cells})
        return {"layers": [x["name"] for x in self.conv["layers"]], "rows": rows}


@lru_cache(maxsize=2)
def _load_cached(config_dir: str, _mtimes: tuple[float, float]) -> Catalog:
    return Catalog.load(Path(config_dir))


def get_catalog(config_dir: str) -> Catalog:
    """Cached, but reloaded when catalog.json or library.yaml change (no restart needed after catalog-ids)."""
    d = Path(config_dir)
    return _load_cached(config_dir, ((d / "catalog.json").stat().st_mtime, (d / "library.yaml").stat().st_mtime))

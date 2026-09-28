#!/usr/bin/env python3
"""Give config/catalog.json explicit, stable ids (layer, family, slot). Idempotent: existing ids are never changed.

New families/slots get ids from the naming convention (config/library.yaml):
  family = <biome prefix><role>_<slug(family name)>      e.g. forest_prop_containers_storage
  slot   = <family>_<letters>                            e.g. forest_prop_containers_storage_a ... _z, _aa, _ab
Raising a family's `count` appends new slot ids; lowering it is refused (would orphan assigned slots).
Usage: python3 scripts/catalog-ids.py [--check]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG, CONVENTIONS = ROOT / "config" / "catalog.json", ROOT / "config" / "library.yaml"
STOPWORDS = {"and", "the", "of"}


def slug(name: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", name.lower().replace("&", " ")).split()
    return "_".join([w for w in words if w not in STOPWORDS][:2])


def letters(k: int) -> str:
    """0 -> a, 25 -> z, 26 -> aa, 27 -> ab (spreadsheet-style)."""
    out = ""
    k += 1
    while k:
        k, r = divmod(k - 1, 26)
        out = chr(97 + r) + out
    return out


def assign_ids(data: dict, conv: dict) -> int:
    added = 0
    for b in data["biomes"]:
        prefix = conv["biomes"][b["id"]]["prefix"]
        for li, layer in enumerate(b["layers"]):
            role = conv["layers"][li]["role"]
            if "id" not in layer:
                layer["id"], added = f"{prefix}{role}", added + 1
            for fam in layer["families"]:
                if "id" not in fam:
                    fam["id"], added = f"{prefix}{role}_{slug(fam['name'])}", added + 1
                slots = fam.setdefault("slots", [])
                if len(slots) > fam["count"]:
                    sys.exit(f"{fam['id']}: count {fam['count']} < {len(slots)} existing slot ids; remove ids explicitly")
                while len(slots) < fam["count"]:
                    slots.append(f"{fam['id']}_{letters(len(slots))}")
                    added += 1
    return added


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 1 if any id is missing (CI)")
    args = ap.parse_args()
    raw = json.loads(CATALOG.read_text())
    data = {"version": 2, "biomes": raw} if isinstance(raw, list) else raw  # v1 was a bare list
    conv = yaml.safe_load(CONVENTIONS.read_text())
    added = assign_ids(data, conv)
    if args.check:
        sys.exit(1 if added or isinstance(raw, list) else 0)
    CATALOG.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"assigned {added} new ids -> {CATALOG.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

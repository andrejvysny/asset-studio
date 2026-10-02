"""Group same-species assets of a library into families through the Studio API (local auth mode, no token).

Rule: family key = first `_` token of the asset's name_id; keys with >= 2 assets become a family. Idempotent: a
repeat only attaches assets that are not yet in a family. Assets already in another family are left alone.

    uv run python scripts/group_families.py --library prj_... [--base-url http://127.0.0.1:8190] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict

import httpx

DISPLAY = {"dead": "Dead tree"}  # first-token keys whose title-cased form would read badly


def family_name(key: str) -> str:
    return DISPLAY.get(key, key.capitalize())


def plan(assets: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for a in assets:
        if a.get("kind") == "model3d" and not a.get("family_id"):
            groups[a["name_id"].split("_")[0]].append(a)
    return {k: sorted(v, key=lambda a: a["name_id"]) for k, v in sorted(groups.items()) if len(v) >= 2}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--library", required=True)
    p.add_argument("--base-url", default="http://127.0.0.1:8190")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    api = f"{args.base_url}/api/v1/projects/{args.library}"
    with httpx.Client(timeout=60, headers={"x-assetstudio": "1"}) as c:
        r = c.get(f"{api}/assets", params={"planned": "false", "limit": 500})
        r.raise_for_status()
        groups = plan(r.json()["items"])
        for key, members in groups.items():
            print(f"{family_name(key):12} {len(members):3} assets")
            if args.dry_run:
                continue
            body = {"name": family_name(key), "asset_ids": [a["asset_id"] for a in members]}
            out = c.post(f"{api}/families", json=body)
            out.raise_for_status()
            if out.json()["skipped"]:
                print(f"  skipped: {out.json()['skipped']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

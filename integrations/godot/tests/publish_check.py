#!/usr/bin/env python3
"""Reference check of one publish build directory (source.zip, portable.glb, descriptor.json, conversion_report.json)
with the SERVER's own publication checks (assetstudio_server.services.source_publication_checks): GLB static/self
contained, draft schema, surfaces exist, anchor within bounds, source package grammar, draft/manifest agreement.

Usage (from the repo root): uv run python integrations/godot/tests/publish_check.py <dir>
Prints one JSON object; exit 0 when the server would accept the preview, 1 with the error code otherwise.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from assetstudio_server.services import source_publication_checks as chk
from assetstudio_server.services.principals import ServiceError

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "godot-integration" / "v1"


def check(build: Path) -> dict:
    caps = json.loads((CONTRACTS / "capabilities.json").read_text())
    glb = chk.check_glb((build / "portable.glb").read_bytes(), caps["limits"])
    draft = chk.parse_draft_part((build / "descriptor.json").read_bytes())
    chk.check_surfaces(draft, glb.doc)
    chk.check_anchor(draft, glb)
    with tempfile.TemporaryDirectory() as work:
        facts = chk.check_source(build / "source.zip", Path(work) / "x", caps, lambda _k, _d: None)
    report = chk.parse_report((build / "conversion_report.json").read_bytes())
    surfaces = chk.check_agreement(draft, facts, report)
    return {"ok": True, "detected": facts.report.detected_capabilities, "declared": facts.manifest.capabilities,
            "warnings": sorted(set(glb.warnings) | set(facts.warnings) | set(draft.preview_warnings)),
            "slots": [s.slot_id for s in draft.material_slots], "source_surfaces": surfaces,
            "evidence": facts.evidence, "budget": glb.budget["within_ipad_budget"],
            "files": [f.path for f in facts.manifest.files], "status": report.portable_status}


def main() -> int:
    try:
        print(json.dumps(check(Path(sys.argv[1])), sort_keys=True))
        return 0
    except ServiceError as e:
        print(json.dumps({"ok": False, "code": e.code, "message": str(e), "details": e.details}, default=str))
        return 1


if __name__ == "__main__":
    sys.exit(main())

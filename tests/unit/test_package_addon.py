"""scripts/package_addon.py: deterministic archive + manifest, and nothing private or test-only is packaged."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = re.compile(r"(^|/)(tests?|\.godot|\.claude|\.git|\.assetstudio|__pycache__|secrets)(/|$)|"
                       r"\.(pyc|token|secret|key|pem|tmp)$|(^|/)(\.env|connections\.json|\.DS_Store)$")


@pytest.fixture(scope="module")
def pkg():
    spec = importlib.util.spec_from_file_location("package_addon", ROOT / "scripts" / "package_addon.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_two_builds_are_byte_identical(pkg, tmp_path: Path) -> None:
    a, _ = pkg.build(tmp_path / "a")
    b, _ = pkg.build(tmp_path / "b")
    assert a.read_bytes() == b.read_bytes()
    ma = (tmp_path / "a" / f"assetstudio-addon-{pkg.read_version()}.manifest.json").read_bytes()
    mb = (tmp_path / "b" / f"assetstudio-addon-{pkg.read_version()}.manifest.json").read_bytes()
    assert ma == mb


def test_manifest_matches_archive(pkg, tmp_path: Path) -> None:
    zip_path, digest = pkg.build(tmp_path)
    manifest = json.loads((tmp_path / f"assetstudio-addon-{pkg.read_version()}.manifest.json").read_text())
    assert manifest["version"] == pkg.read_version()
    assert manifest["contract_version"] == 1
    assert manifest["archive"] == {"name": zip_path.name, "sha256": digest}
    assert set(manifest["source"]) == {"commit", "dirty"}
    with zipfile.ZipFile(zip_path) as zf:
        assert sorted(zf.namelist()) == list(manifest["files"])
        for name, sha in manifest["files"].items():
            assert hashlib.sha256(zf.read(name)).hexdigest() == sha


def test_archive_excludes_private_and_test_files(pkg) -> None:
    entries = pkg.collect()
    assert entries and not [n for n in entries if FORBIDDEN.search(n)]


def test_skip_rules(pkg) -> None:
    for rel in ("tests/a.gd", ".godot/x.cfg", ".claude/m.md", "core/__pycache__/a.pyc", "token.token", ".env",
                "sub/.assetstudio/c.json"):
        assert pkg._skipped(pkg.ADDON / rel), rel
    assert not pkg._skipped(pkg.ADDON / "core" / "as_version.gd")

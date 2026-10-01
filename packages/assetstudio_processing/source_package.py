"""Server-side static validator for untrusted GodotStaticSourcePackageV1 ZIPs (AS-04).

Pure and bounded: no Godot engine, no eval, no class instantiation. Grammar and error mapping:
contracts/godot-integration/v1/static-source-package.md. Every claim of the archive is rechecked.
"""
from __future__ import annotations

import posixpath
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, BinaryIO

from assetstudio_core.delivery import strict_loads
from assetstudio_core.source_manifest import MANIFEST_NAME, SourcePackageManifestV1, parse_source_manifest

from .source_report import Problem, ProblemSink, SourcePackageError, SourcePackageReport
from .source_scene import PackageChecker
from .source_zip import extract_member, scan_archive

__all__ = ["Problem", "SourcePackageReport", "validate_source_package"]

MAX_MANIFEST_BYTES = 8 * 1024 * 1024
DependencyResolver = Callable[[str, dict[str, Any]], str | None]


def validate_source_package(
    zip_path: Path, staging_dir: Path, *, capabilities: Mapping[str, Any],
    resolve_dependency: DependencyResolver | None = None,
) -> SourcePackageReport:
    """Validate `zip_path`, streaming members into the caller-owned, empty `staging_dir`.

    ZIP-layer and manifest failures stop at the first problem; content problems are collected (max 100).
    The staging directory holds partial output after a failure and is discarded by the caller.
    """
    sink, report = ProblemSink(), SourcePackageReport()
    try:
        _validate(Path(zip_path), Path(staging_dir), capabilities, resolve_dependency, report, sink)
    except SourcePackageError as e:
        sink.add(e.problem)
    report.errors, report.warnings = sink.errors, sink.warnings
    report.ok = not sink.errors
    return report


def _validate(zip_path: Path, staging: Path, caps: Mapping[str, Any], resolver: DependencyResolver | None,
              report: SourcePackageReport, sink: ProblemSink) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    if any(staging.iterdir()):
        raise ValueError("staging_dir must be empty")
    size = zip_path.stat().st_size
    with zip_path.open("rb") as fp:
        members = scan_archive(fp, caps["limits"])
        manifest = _load_manifest(fp, size, members, staging, report, sink)
        if manifest is None:
            return
        _check_member_set(members, manifest, caps["source_package"], sink)
        if sink.errors:
            return
        _extract_declared(fp, size, members, manifest, staging, report, sink)
    if sink.errors:
        return
    facts = PackageChecker(manifest, staging, caps["source_package"], sink).run()
    report.detected_capabilities = facts.detected
    report.dependency_closure = facts.reached
    report.asset_dependencies = facts.deps
    report.structure = facts.structure
    if "shader_source" in facts.detected:
        sink.warn("shader_source_desktop_trust", "package contains shader source: desktop trust required, "
                  "not executed on iPad")
    _resolve_dependencies(manifest, facts.deps, resolver, sink)


def _load_manifest(fp: BinaryIO, size: int, members: dict[str, zipfile.ZipInfo], staging: Path,
                   report: SourcePackageReport, sink: ProblemSink) -> SourcePackageManifestV1 | None:
    info = members.get(MANIFEST_NAME)
    if info is None:
        raise SourcePackageError("unsafe_package", "manifest_missing", f"{MANIFEST_NAME} is missing", MANIFEST_NAME)
    if info.file_size > MAX_MANIFEST_BYTES:
        raise SourcePackageError("resource_limit", "manifest_size", f"{MANIFEST_NAME} larger than 8 MiB", MANIFEST_NAME)
    digest, written = extract_member(fp, info, staging, size)
    report.manifest_sha256, report.expanded_bytes = digest, written
    raw = (staging / MANIFEST_NAME).read_bytes()
    try:
        manifest = parse_source_manifest(raw)
    except (ValueError, RecursionError) as e:
        if _unknown_dependency_keys(raw, sink):
            return None
        raise SourcePackageError("unsafe_package", "manifest_invalid", str(e)[:500], MANIFEST_NAME) from e
    report.manifest = manifest
    return manifest


def _unknown_dependency_keys(raw: bytes, sink: ProblemSink) -> bool:
    """The typed model rejects an unknown asset_key as a whole; surface it with its dedicated error code."""
    try:
        doc = strict_loads(raw)
        rmap, deps = doc["resource_map"], doc["asset_dependencies"]
        entries = [(res, e) for res, e in rmap.items() if e.get("kind") == "asset_dependency"]
        missing = [(res, e["asset_key"]) for res, e in entries if e["asset_key"] not in deps]
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return False
    for res, key in missing:
        sink.error("unsupported_source_dependency", "unknown_asset_key",
                   f"resource_map[{res}] names asset_key {key} which is not in asset_dependencies", MANIFEST_NAME)
    return bool(missing)


def _check_member_set(members: dict[str, zipfile.ZipInfo], manifest: SourcePackageManifestV1,
                      policy: Mapping[str, Any], sink: ProblemSink) -> None:
    declared = {f.path for f in manifest.files}
    names = set(members) - {MANIFEST_NAME}
    for name in sorted(names - declared):
        sink.error("unsafe_package", "undeclared_member", "member is not listed in source_manifest.json", name)
    for name in sorted(declared - names):
        sink.error("unsafe_package", "missing_member", "declared file is not in the archive", name)
    for name in sorted(declared & names):
        problem = _extension_problem(name, policy)
        if problem:
            sink.error("unsafe_package", problem[0], problem[1], name)


def _extension_problem(name: str, policy: Mapping[str, Any]) -> tuple[str, str] | None:
    base, lower = posixpath.basename(name).lower(), name.lower()
    for entry in policy["forbidden_extensions"]:
        if entry.startswith(".") and lower.endswith(entry):
            detail = "binary_resource" if entry in (".scn", ".res") else "forbidden_extension"
            return detail, f"{entry} members are forbidden"
        if not entry.startswith(".") and base == entry:
            return "forbidden_file", f"{entry} members are forbidden"
    if posixpath.splitext(name)[1] not in policy["allowed_extensions"]:
        return "extension_not_allowed", "file extension is not allowed"
    return None


def _extract_declared(fp: BinaryIO, size: int, members: dict[str, zipfile.ZipInfo],
                      manifest: SourcePackageManifestV1, staging: Path, report: SourcePackageReport,
                      sink: ProblemSink) -> None:
    for entry in manifest.files:
        digest, written = extract_member(fp, members[entry.path], staging, size)
        report.files[entry.path] = {"sha256": digest, "size": written}
        report.expanded_bytes += written
        if written != entry.size:
            sink.error("integrity_mismatch", "size", f"size {written} differs from declared {entry.size}", entry.path)
        if digest != entry.sha256:
            sink.error("integrity_mismatch", "sha256", "SHA-256 differs from the declared hash", entry.path)


def _resolve_dependencies(manifest: SourcePackageManifestV1, used: list[str], resolver: DependencyResolver | None,
                          sink: ProblemSink) -> None:
    if resolver is None:
        return
    for key in used:
        message = resolver(key, manifest.asset_dependencies[key].model_dump(mode="json"))
        if message:
            sink.error("unsupported_source_dependency", "dependency_unavailable", message, key)

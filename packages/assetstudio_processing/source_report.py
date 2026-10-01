"""Result types shared by the source-package validator modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from assetstudio_core.source_manifest import SourcePackageManifestV1

MAX_PROBLEMS = 100


@dataclass(frozen=True)
class Problem:
    code: str  # unsafe_package | resource_limit | integrity_mismatch | unsupported_source_dependency
    detail: str
    path: str | None
    message: str


class SourcePackageError(Exception):
    """A fatal problem that stops validation (ZIP layer, manifest)."""

    def __init__(self, code: str, detail: str, message: str, path: str | None = None) -> None:
        super().__init__(message)
        self.problem = Problem(code, detail, path, message)


class ProblemSink:
    def __init__(self) -> None:
        self.errors: list[Problem] = []
        self.warnings: list[Problem] = []

    def error(self, code: str, detail: str, message: str, path: str | None = None) -> None:
        if len(self.errors) < MAX_PROBLEMS:
            self.errors.append(Problem(code, detail, path, message))

    def add(self, problem: Problem) -> None:
        self.error(problem.code, problem.detail, problem.message, problem.path)

    def warn(self, detail: str, message: str, path: str | None = None) -> None:
        if len(self.warnings) < MAX_PROBLEMS:
            self.warnings.append(Problem("warning", detail, path, message))


@dataclass
class SourcePackageReport:
    ok: bool = False
    errors: list[Problem] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)
    manifest: SourcePackageManifestV1 | None = None
    manifest_sha256: str | None = None
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    detected_capabilities: list[str] = field(default_factory=list)
    dependency_closure: list[str] = field(default_factory=list)
    asset_dependencies: list[str] = field(default_factory=list)
    expanded_bytes: int = 0

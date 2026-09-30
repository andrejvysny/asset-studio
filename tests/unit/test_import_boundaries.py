"""Package dependency direction (Modular_system_spec.html §20, docs/modular/compute-runner.md): core imports no
transport/runtime/GPU code, the protocol stays transport-free, runners never import the Studio application."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

PACKAGES = {
    "assetstudio_core": ROOT / "packages/assetstudio_core",
    "assetstudio_storage": ROOT / "packages/assetstudio_storage",
    "assetstudio_processing": ROOT / "packages/assetstudio_processing",
    "assetstudio_protocol": ROOT / "packages/assetstudio_protocol",
    "assetstudio_client": ROOT / "packages/assetstudio_client",
    "assetstudio_server": ROOT / "services/studio/assetstudio_server",
    "assetstudio_node": ROOT / "services/compute_node/assetstudio_node",
}
RUNTIMES = {"fastapi", "starlette", "uvicorn", "httpx", "torch", "bpy", "trellis2"}
INTERNAL = set(PACKAGES)

FORBIDDEN = {
    "assetstudio_core": RUNTIMES | INTERNAL - {"assetstudio_core"},
    "assetstudio_storage": RUNTIMES | {"assetstudio_processing", "assetstudio_protocol", "assetstudio_client",
                                      "assetstudio_server", "assetstudio_node"},
    "assetstudio_processing": RUNTIMES | {"assetstudio_protocol", "assetstudio_client", "assetstudio_server",
                                         "assetstudio_node"},
    "assetstudio_protocol": RUNTIMES | INTERNAL - {"assetstudio_core", "assetstudio_protocol"},
    "assetstudio_client": {"fastapi", "starlette", "uvicorn", "torch", "bpy", "assetstudio_storage",
                           "assetstudio_processing", "assetstudio_server", "assetstudio_node"},
    "assetstudio_node": {"assetstudio_server"},
    "assetstudio_server": {"assetstudio_node"},
}
# Transitional direct-mode shims (docs/modular/migration.md): "<server module>" -> allowed import. Emptied in WP2.10.
ALLOWED: dict[str, set[str]] = {}


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".")[0])
    return out


@pytest.mark.parametrize("package", sorted(PACKAGES))
def test_dependency_direction(package: str) -> None:
    root = PACKAGES[package]
    assert (root / "__init__.py").exists(), f"{package} must be an importable package"
    violations = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        bad = (_imports(path) & FORBIDDEN[package]) - ALLOWED.get(rel, set())
        violations += [f"{rel} imports {name}" for name in sorted(bad)]
    assert not violations, "\n".join(violations)

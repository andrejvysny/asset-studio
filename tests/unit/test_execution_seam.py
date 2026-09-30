"""Execution seam: engine access goes through studio.execution; ids and call scopes behave."""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from assetstudio_core.ids import derived_id
from assetstudio_server.coordinator.runner import TaskEnv
from assetstudio_server.execution import DirectBackend
from assetstudio_server.studio import build_studio

from tests.conftest import ROOT, make_settings

SERVER = ROOT / "services" / "studio" / "assetstudio_server"
SINGLETONS = {"engine", "aux", "worker3d"}
# Only the backend itself and the container may read the singletons directly.
ALLOWED = {"execution.py", "studio.py"}


def _direct_reads(path: Path) -> list[int]:
    hits = []
    for n in ast.walk(ast.parse(path.read_text())):
        if (isinstance(n, ast.Attribute) and n.attr in SINGLETONS and isinstance(n.ctx, ast.Load)
                and isinstance(n.value, ast.Name | ast.Attribute)):
            base = n.value.id if isinstance(n.value, ast.Name) else n.value.attr
            if base == "studio":
                hits.append(n.lineno)
    return hits


def test_no_direct_singleton_reads_outside_seam() -> None:
    offenders = {str(p.relative_to(SERVER)): lines for p in SERVER.rglob("*.py")
                 if p.name not in ALLOWED and (lines := _direct_reads(p))}
    assert offenders == {}


class _Stub:
    mode = "stub"
    simulated = False

    def __init__(self, gen: int) -> None:
        self.gen = gen
        self.asked: list[str] = []

    def generation(self, env: Any, call_key: str) -> int:
        self.asked.append(call_key)
        return self.gen


class _Holder:
    def __init__(self, execution: Any) -> None:
        self.execution = execution


def _env(execution: Any) -> TaskEnv:
    return TaskEnv(studio=_Holder(execution), ctx=None, task=None)  # type: ignore[arg-type]


def test_direct_backend_reads_live_objects(tmp_path: Path) -> None:
    studio = build_studio(make_settings(tmp_path, coordinator=False))
    try:
        be = studio.execution
        assert isinstance(be, DirectBackend) and be.mode == "direct"
        assert be.engine() is studio.engine and be.worker3d() is studio.worker3d
        assert be.aux() is studio.aux and be.aux() is not None
        studio.aux = None
        assert be.aux() is None and studio.simulated == be.simulated
        assert be.generation(_env(be), "0") == 1
    finally:
        studio.close()


def test_call_scope_nests() -> None:
    env = _env(_Stub(1))
    assert env.current_call is None
    with env.call("a"):
        with env.call("b"):
            assert env.current_call == "b"
        assert env.current_call == "a"
    assert env.current_call is None
    with pytest.raises(RuntimeError), env.call("x"):
        raise RuntimeError
    assert env.current_call is None


def test_output_id_generations() -> None:
    legacy = derived_id("art", "tsk_1", "0")
    g1 = _Stub(1)
    assert _env(g1).output_id("art", "tsk_1", "0", call="0") == legacy
    assert g1.asked == ["0"]
    assert _env(g1).output_id("art", "tsk_1", "0") == legacy and g1.asked == ["0"]
    g2 = _env(_Stub(2))
    moved = g2.output_id("art", "tsk_1", "0", call="0")
    assert moved == derived_id("art", "tsk_1", "0", "g2") != legacy
    assert g2.output_id("art", "tsk_1", "0") == legacy  # no call key: never re-placed


def test_nodes_mode_not_yet_available(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    s.execution = "nodes"
    with pytest.raises(ValueError, match=r"node execution arrives with the remote adapters \(WP2\.4\)"):
        build_studio(s)

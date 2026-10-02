"""Chaos harness: the container killed is the one running the observed attempt's runner."""
from __future__ import annotations

import pytest

from tests.gpu_nodes.conftest import runner_service

A, B = {"id": "run_a", "name": "node-a"}, {"id": "run_b", "name": "node-b"}


def test_single_runner_is_the_compose_runner_service() -> None:
    assert runner_service([A], "run_a") == "runner"


def test_mapping_selects_the_owning_runner_service() -> None:
    assert runner_service([A, B], "run_b", "node-a=runner,node-b=runner-b") == "runner-b"


def test_ambiguous_or_inactive_owner_is_refused_not_guessed() -> None:
    with pytest.raises(AssertionError, match="GPU_NODES_RUNNER_SERVICES"):
        runner_service([A, B], "run_b")
    with pytest.raises(AssertionError, match="not active"):
        runner_service([A], "run_gone")

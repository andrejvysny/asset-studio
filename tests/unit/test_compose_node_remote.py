"""compose.node-remote.yml and config/runner.remote.yaml stay consistent."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from assetstudio_core.safeyaml import load_yaml
from assetstudio_node.config import RunnerConfig

ROOT = Path(__file__).resolve().parents[2]


class _Loader(yaml.SafeLoader):
    pass


def _tagged(tag: str):
    def construct(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
        if isinstance(node, yaml.SequenceNode):
            return {tag: loader.construct_sequence(node, deep=True)}
        return {tag: loader.construct_object(node, deep=True)}

    return construct


_Loader.add_constructor("!override", _tagged("!override"))
_Loader.add_constructor("!reset", _tagged("!reset"))


def _load(name: str) -> dict[str, Any]:
    return yaml.load((ROOT / name).read_text(), Loader=_Loader)  # noqa: S506 - SafeLoader subclass


def test_node_remote_has_runner_and_no_studio() -> None:
    doc = _load("compose.node-remote.yml")
    assert "studio" not in doc["services"] and "runner" in doc["services"]
    runner = doc["services"]["runner"]
    assert any(v.endswith(":/etc/assetstudio/runner.yaml:ro") and "runner.remote.yaml" in v
               for v in runner["volumes"])
    assert runner["secrets"][0]["source"] == "runner_registration_token"
    assert any(v.endswith(":/var/lock/assetstudio-runner") and v.startswith("${RUNNER_HOST_LOCK_DIR:-/")
               for v in runner["volumes"])


def test_remote_runner_config_validates() -> None:
    cfg = RunnerConfig.model_validate(load_yaml((ROOT / "config" / "runner.remote.yaml").read_text()))
    assert cfg.studio_url.startswith("https://") and cfg.dispatch == "pull"
    assert [s.capability for s in cfg.slots] == ["aux3d"]
    assert cfg.host_lock == Path("/var/lock/assetstudio-runner/host.lock")

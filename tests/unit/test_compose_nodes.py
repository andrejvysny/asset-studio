"""compose.nodes.yml (profile S) and config/runner.single.yaml stay consistent."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
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


def test_studio_has_no_models_and_runs_nodes() -> None:
    studio = _load("compose.nodes.yml")["services"]["studio"]
    assert studio["environment"]["STUDIO_EXECUTION"] == "nodes"
    assert studio["environment"]["STUDIO_PUBLIC_URL"] == "http://studio:8190"
    for key in ("COMFY_URL", "AUX_URL", "WORKER3D_URL"):
        assert studio["environment"][key] == ""
    vols = studio["volumes"]["!override"]
    assert vols and not any("/models" in v for v in vols)


def test_comfyui_port_reset() -> None:
    assert _load("compose.nodes.yml")["services"]["comfyui"]["ports"] == {"!reset": []}


def test_runner_service() -> None:
    doc = _load("compose.nodes.yml")
    base = _load("compose.yml")
    runner = doc["services"]["runner"]
    assert runner["image"] == base["services"]["studio"]["image"]
    assert runner["networks"] == ["internal"]
    assert any(v.endswith(":/models:ro") for v in runner["volumes"])
    assert any(v.startswith("runner-state:") for v in runner["volumes"])
    assert runner["secrets"][0]["source"] == "runner_registration_token"
    assert "runner_registration_token" in doc["secrets"]
    assert "runner-state" in doc["volumes"]
    caps = runner["deploy"]["resources"]["reservations"]["devices"][0]["capabilities"]
    assert caps == ["utility"]


def test_runner_config_validates() -> None:
    cfg = RunnerConfig.model_validate(yaml.safe_load((ROOT / "config/runner.single.yaml").read_text()))
    assert {s.slot_id for s in cfg.slots} == {"image", "aux3d"}
    devices = [d for s in cfg.slots for d in s.devices]
    assert len(devices) == len(set(devices))

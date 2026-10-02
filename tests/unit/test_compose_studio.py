"""compose.yml (Studio only), Compose >= 2.24.4."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

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


def _text(name: str) -> str:
    return (ROOT / name).read_text()


def test_studio_only_has_no_gpu_engine_runner_or_models() -> None:
    doc = _load("compose.yml")
    assert list(doc["services"]) == ["studio"]
    studio = doc["services"]["studio"]
    assert "deploy" not in studio and "devices" not in studio
    assert studio["environment"]["STUDIO_EXECUTION"] == "nodes"
    assert studio["networks"] == ["internal", "public"] and doc["networks"]["internal"]["internal"] is True
    assert "internal" not in doc["networks"]["public"]  # published ports need a non-internal network
    assert "healthcheck" in studio
    assert not any("/models" in v for v in studio["volumes"])
    assert any(v.endswith(":/data/instance") for v in studio["volumes"])
    for key in ("COMFY_URL", "AUX_URL", "WORKER3D_URL"):
        assert studio["environment"][key] == ""
    text = _text("compose.yml").lower()
    assert "nvidia" not in text and "comfyui:" not in text and "runner:" not in text.replace("runners", "")


def test_compose_minimum_is_2_24_4_everywhere() -> None:
    for name in ("compose.yml", "compose.nodes.yml", "compose.node-remote.yml"):
        assert "2.24.4" in _text(name), name
    assert "2.24.4" in _text("Makefile")
    assert "2.24.4" in (ROOT / "docs" / "installation.md").read_text()


def test_studio_ports_default_to_loopback() -> None:
    studio = _load("compose.yml")["services"]["studio"]
    ui = next(p for p in studio["ports"] if p.endswith(":8190"))
    assert ui.startswith("127.0.0.1:")  # no operator auth in local mode: never published on a reachable address
    for port in studio["ports"]:
        assert port.startswith(("127.0.0.1:", "${MCP_BIND:-127.0.0.1}", "${INTEGRATION_BIND:-127.0.0.1}")), port

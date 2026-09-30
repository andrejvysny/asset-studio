"""compose.public.yml (profile P) and compose.node-remote.yml stay consistent with the Studio auth design."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from assetstudio_core.safeyaml import load_yaml
from assetstudio_node.config import RunnerConfig

ROOT = Path(__file__).resolve().parents[2]
IDENTITY = ("Remote-User", "Remote-Groups", "X-AssetStudio-Proxy-Secret")


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


def _labels() -> dict[str, str]:
    return _load("compose.public.yml")["services"]["studio"]["labels"]


def _middlewares(router: str) -> list[str]:
    return _labels()[f"traefik.http.routers.{router}.middlewares"].split(",")


def test_studio_has_no_host_port_and_uses_proxy_auth() -> None:
    doc = _load("compose.public.yml")
    studio = doc["services"]["studio"]
    assert studio["ports"] == {"!reset": []}
    assert studio["environment"]["STUDIO_AUTH_MODE"] == "proxy"
    assert studio["environment"]["STUDIO_PROXY_SECRET_FILE"] == "/run/secrets/studio_proxy_secret"
    assert studio["environment"]["STUDIO_PUBLIC_URL"].startswith("https://")
    assert "proxy" in studio["networks"] and doc["networks"]["proxy"]["external"] is True
    assert "studio_proxy_secret" in doc["secrets"]


def test_browser_router_uses_authelia_after_header_strip() -> None:
    mws = _middlewares("assetstudio")
    assert mws[0] == "assetstudio-headers" and any(m.startswith("${AUTHELIA_MIDDLEWARE") for m in mws)


def test_runner_routers_bypass_authelia_and_are_limited() -> None:
    labels = _labels()
    for router in ("assetstudio-runner", "assetstudio-runner-open", "assetstudio-runner-transfer"):
        mws = _middlewares(router)
        assert not any("authelia" in m.lower() for m in mws), router
        assert mws[0] == "assetstudio-headers", router
    assert "PathPrefix(`/api/runner/`)" in labels["traefik.http.routers.assetstudio-runner.rule"]
    assert any("-rl" in m for m in _middlewares("assetstudio-runner"))
    assert any("-rl" in m for m in _middlewares("assetstudio-runner-open"))
    assert any("inflight" in m for m in _middlewares("assetstudio-runner-transfer"))
    prio = {r: int(labels[f"traefik.http.routers.{r}.priority"])
            for r in ("assetstudio-runner", "assetstudio-runner-open", "assetstudio-runner-transfer")}
    assert min(prio.values()) > 0 and prio["assetstudio-runner-open"] > prio["assetstudio-runner"]
    assert prio["assetstudio-runner-transfer"] > prio["assetstudio-runner"]


def test_identity_headers_stripped_and_secret_set() -> None:
    labels = _labels()
    prefix = "traefik.http.middlewares.assetstudio-headers.headers.customrequestheaders."
    assert labels[prefix + "Remote-User"] == "" and labels[prefix + "Remote-Groups"] == ""
    assert labels[prefix + "X-AssetStudio-Proxy-Secret"].startswith("${STUDIO_PROXY_SECRET")
    assert all(prefix + h in labels for h in IDENTITY)


def test_node_remote_has_runner_and_no_studio() -> None:
    doc = _load("compose.node-remote.yml")
    assert "studio" not in doc["services"] and "runner" in doc["services"]
    runner = doc["services"]["runner"]
    assert any(v.endswith(":/etc/assetstudio/runner.yaml:ro") and "runner.remote.yaml" in v
               for v in runner["volumes"])
    assert runner["secrets"][0]["source"] == "runner_registration_token"


def test_remote_runner_config_validates() -> None:
    cfg = RunnerConfig.model_validate(load_yaml((ROOT / "config" / "runner.remote.yaml").read_text()))
    assert cfg.studio_url.startswith("https://") and cfg.dispatch == "pull"
    assert [s.capability for s in cfg.slots] == ["aux3d"]

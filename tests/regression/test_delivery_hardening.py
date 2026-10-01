"""Delivery preparation hardening: verified source capabilities (profile v2), lock-free dependency closure, and
per-representation readiness."""
from __future__ import annotations

import shutil
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from assetstudio_server.errors import ApiError
from assetstudio_server.services import deliveries as svc
from assetstudio_storage import delivery as store_delivery

from tests.conftest import new_project
from tests.integration_publication_support import PubEnv, cluster_zip, commit_body, glb_parts, make_env, source_parts
from tests.integration_support import client, make_token

PORTABLE, SOURCE = svc.PORTABLE, svc.SOURCE
DELIVERY_DIRS = ("deliveries", "descriptors", "delivery_index", "delivery_artifacts")


@pytest.fixture
def env(make_api) -> PubEnv:
    return make_env(make_api)


def _src(done: dict[str, Any]) -> dict[str, Any]:
    return next(d for d in done["deliveries"] if d["representation"] == SOURCE)


def _manifest(env: PubEnv, delivery_id: str, lib: str | None = None) -> dict[str, Any]:
    r = env.c.get(env.url(f"/deliveries/{delivery_id}/manifest", lib))
    assert r.status_code == 200, r.text
    return r.json()


def _resolve(env: PubEnv, done: dict[str, Any], reps: list[str]) -> dict[str, Any]:
    r = env.c.post(env.url("/resolve", done["asset_ref"]["library_id"]),
                   json={"refs": [done["asset_ref"]], "target": {"representations": reps}})
    assert r.status_code == 200, r.text
    return r.json()["entries"][0]


@pytest.mark.parametrize(("name", "cap"), [("csg_hut", "csg_static"), ("custom_shader_crystal", "shader_source"),
                                           ("vertex_color_rock_with_collision", "static_collision")])
def test_source_manifest_lists_every_verified_capability(env: PubEnv, name: str, cap: str) -> None:
    done = env.commit(env.preview(source_parts(name)), f"caps-{name}"[:40], name=name)
    manifest = _manifest(env, _src(done)["delivery_id"])
    assert manifest["profile_id"] == "published_descriptor" and manifest["profile_version"] == "2"
    assert {"godot_text_scene_v1", cap} <= set(manifest["required_capabilities"])


def test_source_capabilities_ignore_descriptor_claim_and_fall_back_to_declared() -> None:
    detected = SimpleNamespace(validation={"source": {"detected_capabilities": ["static_collision", "csg_static"]}})
    assert svc.source_capabilities(detected, lambda: pytest.fail("declared not needed")) == [
        "godot_text_scene_v1", "csg_static", "static_collision"]
    legacy = SimpleNamespace(validation={})
    assert svc.source_capabilities(legacy, lambda: ["shader_source", "bogus"]) == ["godot_text_scene_v1",
                                                                                   "shader_source"]


def test_old_v1_source_delivery_is_kept_and_not_reused(env: PubEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as mp:  # reproduce what the previous code stored
        mp.setattr(svc, "SOURCE_PROFILE", ("published_descriptor", "1"))
        mp.setattr(svc, "source_capabilities", lambda *_: ["godot_text_scene_v1"])
        done = env.commit(env.preview(source_parts("csg_hut")), "old-source-1", name="old")
    old = _src(done)
    assert old["profile_version"] == "1"
    old_bytes = env.c.get(env.url(f"/deliveries/{old['delivery_id']}/manifest")).content
    entry = _resolve(env, done, [SOURCE])
    (new,) = entry["deliveries"]
    assert new["profile_version"] == "2" and new["delivery_id"] != old["delivery_id"]
    assert env.c.get(env.url(f"/deliveries/{old['delivery_id']}/manifest")).content == old_bytes
    assert "csg_static" in _manifest(env, new["delivery_id"])["required_capabilities"]
    ref = done["asset_ref"]
    stored = store_delivery.deliveries(env.ctx().store, ref["asset_id"], ref["version_id"])
    assert {d.profile_version for d in stored if d.representation == SOURCE} == {"1", "2"}


def _wipe(env: PubEnv, lib: str) -> None:
    root = env.ctx(lib).root
    for top in DELIVERY_DIRS:
        shutil.rmtree(root / top, ignore_errors=True)


def _publish(env: PubEnv, c: Any, lib: str, parts: dict[str, bytes], key: str, name: str) -> dict[str, Any]:
    receipt = env.preview(parts, c=c, lib=lib)
    r = c.post(env.url("/publications:commit", lib), json=commit_body(receipt, key, name=name))
    assert r.status_code == 200, r.text
    return r.json()


def _cluster(env: PubEnv, c: Any, lib: str, dep: dict[str, Any], key: str) -> dict[str, Any]:
    parts = {**source_parts("prop_cluster"), "source": cluster_zip(dep["asset_ref"], dep["descriptor_sha256"], None,
                                                                   env.server_id)}
    return _publish(env, c, lib, parts, key, key)


def _two_libraries(env: PubEnv) -> tuple[str, Any]:
    other = new_project(env.api, "Other")
    return other, client(env.app, make_token(env.app, "both", ["assets:read", "assets:publish"], [env.lib, other]))


def test_cross_library_preparation_does_not_deadlock(env: PubEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    lib_b, both = _two_libraries(env)
    a0 = _publish(env, both, env.lib, glb_parts(), "dead-a0-key", "a0")
    b0 = _publish(env, both, lib_b, glb_parts(), "dead-b0-key", "b0")
    a1 = _cluster(env, both, env.lib, b0, "dead-a1-key")  # A1 -> B0
    b1 = _cluster(env, both, lib_b, a0, "dead-b1-key")  # B1 -> A0
    _wipe(env, env.lib)
    _wipe(env, lib_b)  # every version is cold again
    barrier, real = threading.Barrier(2), svc._add_dependency

    def meet(*args: Any, **kw: Any) -> Any:
        barrier.wait(timeout=10)  # both preparations sit inside the dependency step at the same moment
        return real(*args, **kw)

    monkeypatch.setattr(svc, "_add_dependency", meet)
    results: dict[str, Any] = {}

    def prepare(tag: str, lib: str, done: dict[str, Any]) -> None:
        ref = done["asset_ref"]
        try:
            results[tag] = svc.ensure_version(env.api.studio, env.ctx(lib), env.server_id, ref["asset_id"],
                                              ref["version_id"])
        except Exception as e:  # noqa: BLE001
            results[tag] = e

    threads = [threading.Thread(target=prepare, args=("a", env.lib, a1), daemon=True),
               threading.Thread(target=prepare, args=("b", lib_b, b1), daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "deadlock"
    assert all(isinstance(r, svc.EnsureResult) and r.state == "ready" and not r.issues for r in results.values())
    assert {d.representation for d in results["a"].deliveries} == {PORTABLE, SOURCE}


def test_conflicting_requirements_are_rejected() -> None:
    from assetstudio_core.delivery import AssetRef, DeliveryDependency

    ref = AssetRef(server_id="00000000-0000-4000-8000-000000000000", library_id="prj_0000000000000000", asset_id="ast_0000000000000000",
                   version_id="ver_0000000000000000")

    def dep(delivery: str, sha: str = "a" * 64) -> DeliveryDependency:
        return DeliveryDependency(asset_key=ref.key(), asset_ref=ref, descriptor_sha256="b" * 64,
                                  representation=PORTABLE, delivery_id=delivery, manifest_sha256=sha)

    out: dict[str, DeliveryDependency] = {}
    svc._merge(out, dep("dlv_1111111111111111"))
    svc._merge(out, dep("dlv_1111111111111111"))  # identical requirement is fine
    for other in (dep("dlv_2222222222222222"), dep("dlv_1111111111111111", "c" * 64)):
        with pytest.raises(svc.DependencyConflict, match="conflicting requirements for"):
            svc._merge(out, other)
    assert issubclass(svc.DependencyConflict, svc.DependencyUnavailable)


def test_conflict_and_cycle_surface_as_unsupported_source_dependency(env: PubEnv,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    dep = _publish(env, env.c, env.lib, glb_parts(), "conf-dep-key", "dep")
    done = _cluster(env, env.c, env.lib, dep, "conf-top-key")
    _wipe(env, env.lib)

    def reject(*_a: Any, **_k: Any) -> None:
        raise svc.DependencyConflict("conflicting requirements for abcdef123456")

    monkeypatch.setattr(svc, "_add_dependency", reject)
    entry = _resolve(env, done, [SOURCE])
    assert entry["state"] == "unsupported" and entry["error"]["code"] == "unsupported_source_dependency"
    assert "conflicting requirements" in entry["error"]["message"]
    both = _resolve(env, done, [PORTABLE, SOURCE])
    assert both["state"] == "ready" and both["representations"][SOURCE]["error"]["code"] == (
        "unsupported_source_dependency")


def test_dependency_chain_is_bounded(env: PubEnv) -> None:
    ref = _publish(env, env.c, env.lib, glb_parts(), "chain-key-001", "chain")["asset_ref"]
    ctx = env.ctx()
    _wipe(env, env.lib)
    key = f"{ctx.id}/{ref['asset_id']}/{ref['version_id']}"
    args = (env.api.studio, ctx, env.server_id, ref["asset_id"], ref["version_id"])
    with pytest.raises(svc.DependencyUnavailable, match="dependency cycle"):
        svc.ensure_version(*args, _chain=(key,))
    with pytest.raises(svc.DependencyUnavailable, match="too deep"):
        svc.ensure_version(*args, _chain=tuple(f"x/{i}" for i in range(svc.MAX_CHAIN)))


def test_portable_is_ready_while_source_dependency_is_unavailable(env: PubEnv,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    lib_b, both = _two_libraries(env)
    dep = _publish(env, both, lib_b, glb_parts(), "ready-dep-key", "dep")
    done = _cluster(env, both, env.lib, dep, "ready-top-key")
    _wipe(env, env.lib)
    registry = env.api.studio.registry
    real_get = registry.get

    def flaky(project_id: str) -> Any:
        if project_id == lib_b:
            raise ApiError(404, "unknown_project", "gone")
        return real_get(project_id)

    with monkeypatch.context() as mp:
        mp.setattr(registry, "get", flaky)
        only = _resolve(env, done, [PORTABLE])
        assert only["state"] == "ready" and [d["representation"] for d in only["deliveries"]] == [PORTABLE]
        portable = only["deliveries"][0]
        src = _resolve(env, done, [SOURCE])
        assert src["state"] == "temporarily_unavailable" and src["error"]["code"] == "temporarily_unavailable"
        assert f"library {lib_b} unavailable" in src["error"]["message"] and src["deliveries"] == []
        both_entry = _resolve(env, done, [PORTABLE, SOURCE])
        assert both_entry["state"] == "ready" and [d["representation"] for d in both_entry["deliveries"]] == [PORTABLE]
        assert both_entry["representations"][PORTABLE] == {"state": "ready", "error": None}
        assert both_entry["representations"][SOURCE]["state"] == "temporarily_unavailable"
    healed = _resolve(env, done, [PORTABLE, SOURCE])
    assert healed["state"] == "ready" and {d["representation"] for d in healed["deliveries"]} == {PORTABLE, SOURCE}
    assert healed["representations"][SOURCE] == {"state": "ready", "error": None}
    assert next(d for d in healed["deliveries"] if d["representation"] == PORTABLE) == portable


def test_unexpected_representation_is_reported_per_representation(env: PubEnv) -> None:
    done = env.commit(env.preview(glb_parts(2)), "glb-only-rep-1")
    entry = _resolve(env, done, [PORTABLE, SOURCE])
    assert entry["state"] == "ready"
    assert entry["representations"][SOURCE]["error"]["code"] == "unsupported_representation"
    entry = _resolve(env, done, [SOURCE])
    assert entry["state"] == "unsupported" and entry["representations"][SOURCE]["state"] == "unsupported"

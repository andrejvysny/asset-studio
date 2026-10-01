"""Crash injection for integration publication commits: every retry converges on exactly one complete version."""
from __future__ import annotations

from typing import Any

import pytest
from assetstudio_core.domain import AssetManifest, AssetVersion
from assetstudio_core.ids import derived_id
from assetstudio_server.services import source_publications as sp
from assetstudio_storage.project import ProjectStore, manifest_key, version_key

from tests.integration_publication_support import PubEnv, commit_body, make_env, source_parts

KEY = "crash-key-0001"
BOUNDARIES = ["artifact_registration", "version_record", "publish_receipt", "publish", "ensure_version", "index_upsert", "record_command", "cleanup"]


@pytest.fixture
def env(make_api) -> PubEnv:
    return make_env(make_api)


class Crash:
    """Raises once at the chosen boundary of a commit, then lets everything through."""

    def __init__(self, env: PubEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        self.env, self.mp, self.armed = env, monkeypatch, False

    def fire(self) -> None:
        if self.armed:
            self.armed = False
            raise RuntimeError("injected crash")

    def install(self, boundary: str) -> None:
        env, mp = self.env, self.mp
        real_publish, real_ensure, real_register = sp.publish, sp.svc.ensure_version, ProjectStore.register_artifact
        real_upsert, real_record, real_rmtree = env.ctx().index.upsert, env.api.studio.journal.record_command, \
            sp.shutil.rmtree

        def wrap(real: Any, *, before: bool = False, only: Any = None) -> Any:
            def inner(*a: Any, **k: Any) -> Any:
                if only is None or only(*a, **k):
                    if before:
                        self.fire()
                        return real(*a, **k)
                    out = real(*a, **k)
                    self.fire()
                    return out
                return real(*a, **k)
            return inner
        if boundary == "artifact_registration":  # fails half way through the roles (after model/preview/descriptor)
            mp.setattr(ProjectStore, "register_artifact", wrap(
                real_register, before=True, only=lambda self, content, role, *a, **k: role == "conversion_report"))
        elif boundary == "version_record":  # version stored, manifest (the publication point) not yet
            mp.setattr(ProjectStore, "create", wrap(
                ProjectStore.create, before=True, only=lambda self, key, *a, **k: key.startswith("manifests/")))
        elif boundary == "publish_receipt":  # manifest committed, publications/<op>.json not yet
            mp.setattr(ProjectStore, "create_or_same", wrap(
                ProjectStore.create_or_same, before=True,
                only=lambda self, key, *a, **k: key.startswith("publications/")))
        elif boundary == "publish":
            mp.setattr(sp, "publish", wrap(real_publish))
        elif boundary == "ensure_version":
            mp.setattr(sp.svc, "ensure_version", wrap(real_ensure, before=True))
        elif boundary == "index_upsert":
            mp.setattr(env.ctx().index, "upsert", wrap(real_upsert, before=True))
        elif boundary == "record_command":
            mp.setattr(env.api.studio.journal, "record_command", wrap(real_record, before=True))
        else:
            mp.setattr(sp.shutil, "rmtree", wrap(real_rmtree, before=True))
        self.armed = True


def _assert_one_complete_version(env: PubEnv, done: dict[str, Any]) -> None:
    ctx, ref = env.ctx(), done["asset_ref"]
    manifest = ctx.store.get(manifest_key(ref["asset_id"]), AssetManifest)[0]
    assert [v.version_id for v in manifest.versions] == [ref["version_id"]] == [manifest.current_version_id]
    version = ctx.store.get(version_key(ref["asset_id"], ref["version_id"]), AssetVersion)[0]
    assert set(version.artifacts) == {"model", "preview", "descriptor", "godot_source", "conversion_report"}
    for art in version.artifacts.values():
        ctx.store.verify_artifact(art["artifact_id"])
    op = env.c.get(env.url(f"/publication-operations/{KEY}")).json()
    assert op["state"] == "committed" and op["version_id"] == ref["version_id"]
    assert len(env.c.get(env.url("/assets")).json()["items"]) == 1
    detail = env.c.get(env.url(f"/assets/{ref['asset_id']}/versions/{ref['version_id']}")).json()
    assert detail["descriptor"]["state"] == "ready" and len(detail["deliveries"]) == 2


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_retry_after_crash_converges(env: PubEnv, monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    receipt = env.preview(source_parts("primitive_prop"))
    Crash(env, monkeypatch).install(boundary)
    first = env.c.post(env.url("/publications:commit"), json=commit_body(receipt, KEY))
    if boundary == "ensure_version":  # not fatal: published, deliveries prepared later
        assert first.status_code == 200 and first.json()["deliveries"] == []
    else:
        assert first.status_code == 503 and first.json()["error"]["retryable"] is True
    if boundary == "artifact_registration":
        assert env.tree("manifests", "versions", "publications") == {}  # nothing half-published
    again = env.commit(receipt, KEY)
    assert again == env.commit(receipt, KEY)
    expect = derived_id("ver", again["asset_ref"]["asset_id"], derived_id("op", env.lib, "integration", KEY))
    assert again["asset_ref"]["version_id"] == expect and len(again["deliveries"]) == 2
    _assert_one_complete_version(env, again)
    assert env.previews_on_disk() == []


def test_lost_response_is_recoverable_via_operation_lookup(env: PubEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = env.preview(source_parts("primitive_prop"))
    Crash(env, monkeypatch).install("record_command")
    assert env.c.post(env.url("/publications:commit"), json=commit_body(receipt, KEY)).status_code == 503
    op = env.c.get(env.url(f"/publication-operations/{KEY}")).json()
    assert op["state"] == "committed" and op["display_version"] == 1
    monkeypatch.undo()
    again = env.commit(receipt, KEY)
    assert again["asset_ref"]["version_id"] == op["version_id"]


def test_retry_after_staging_sweep_uses_publication_receipt(env: PubEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = env.preview(source_parts("primitive_prop"))
    Crash(env, monkeypatch).install("record_command")
    assert env.c.post(env.url("/publications:commit"), json=commit_body(receipt, KEY)).status_code == 503
    monkeypatch.undo()
    assert sp.sweep_expired(env.api.studio.settings, sp._now().replace(year=2999)) == 1
    assert env.previews_on_disk() == []
    again = env.commit(receipt, KEY)
    assert len(again["deliveries"]) == 2 and again["operation"]["state"] == "committed"
    _assert_one_complete_version(env, again)
    other = env.c.post(env.url("/publications:commit"), json=commit_body(receipt, KEY, name="Changed"))
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_conflict"


def test_existing_asset_crash_keeps_old_pointer_until_complete(env: PubEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    first = env.commit(env.preview(source_parts("primitive_prop")), "first-key-0001")
    aid, v1 = first["asset_ref"]["asset_id"], first["asset_ref"]["version_id"]
    receipt = env.preview(source_parts("primitive_prop_v2"))
    Crash(env, monkeypatch).install("artifact_registration")
    over = {"target_asset_id": aid, "expected_current_version": v1}
    assert env.c.post(env.url("/publications:commit"), json=commit_body(receipt, KEY, **over)).status_code == 503
    manifest = env.ctx().store.get(manifest_key(aid), AssetManifest)[0]
    assert manifest.current_version_id == v1 and len(manifest.versions) == 1
    second = env.commit(receipt, KEY, **over)
    assert second["display_version"] == 2 and env.commit(receipt, KEY, **over) == second

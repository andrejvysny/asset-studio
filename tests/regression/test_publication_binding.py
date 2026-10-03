"""Commit idempotency binds the whole request and the publisher credential; concurrent commits converge."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from assetstudio_core.domain import AssetManifest, AssetVersion
from assetstudio_storage.project import manifest_key, version_key

from tests.integration_publication_support import PubEnv, commit_body, make_env, source_parts
from tests.integration_support import client, make_token

KEY = "binding-key-0001"


@pytest.fixture
def env(make_api) -> PubEnv:
    return make_env(make_api)


def _post(env: PubEnv, receipt: dict[str, Any], c: Any = None, **over: Any) -> Any:
    return (c or env.c).post(env.url("/publications:commit"), json=commit_body(receipt, KEY, **over))


def _crash_record(env: PubEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("injected crash")
    monkeypatch.setattr(env.api.studio.journal, "record_command", boom)


def _conflict(r: Any) -> None:
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_conflict", r.text


CHANGES = [{"name": "Other"}, {"tags": ["x"]}, {"category_id": "cat"}, {"licence": "CC0"}, {"credit": "me"},
           {"source_uri": "https://example.com/a"}, {"target_asset_id": "ast_" + "a" * 26,
                                                      "expected_current_version": "ver_" + "a" * 26},
           {"portable_sha256": "0" * 64}, {"descriptor_draft_sha256": "0" * 64}]


def test_crash_then_changed_request_conflicts_original_converges(env: PubEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = env.preview(source_parts("primitive_prop"))
    _crash_record(env, monkeypatch)
    assert _post(env, receipt).status_code == 503
    monkeypatch.undo()
    for change in CHANGES:
        _conflict(_post(env, receipt, **change))
    done = env.commit(receipt, KEY)
    assert len(env.ctx().store.get(manifest_key(done["asset_ref"]["asset_id"]), AssetManifest)[0].versions) == 1
    assert env.commit(receipt, KEY) == done


def test_other_credential_conflicts_after_crash_and_after_success(env: PubEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    other = client(env.app, make_token(env.app, "other", ["assets:read", "assets:publish"], [env.lib]))
    receipt = env.preview(source_parts("primitive_prop"))
    _crash_record(env, monkeypatch)
    assert _post(env, receipt).status_code == 503
    monkeypatch.undo()
    _conflict(_post(env, receipt, other))
    done = env.commit(receipt, KEY)
    _conflict(_post(env, receipt, other))
    assert env.commit(receipt, KEY) == done


def test_concurrent_identical_commits_converge(env: PubEnv) -> None:
    receipt = env.preview(source_parts("primitive_prop"))
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: _post(env, receipt), range(4)))
    assert [r.status_code for r in results] == [200] * 4, [r.text for r in results]
    assert all(r.json() == results[0].json() for r in results)
    aid = results[0].json()["asset_ref"]["asset_id"]
    assert len(env.ctx().store.get(manifest_key(aid), AssetManifest)[0].versions) == 1


def test_recreated_token_name_cannot_commit_old_preview(env: PubEnv) -> None:
    receipt = env.preview(source_parts("primitive_prop"))
    assert env.app.fastapi.state.tokens.revoke("godot")
    fresh = client(env.app, make_token(env.app, "godot", ["assets:read", "assets:publish"], [env.lib]))
    r = _post(env, receipt, fresh)
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden", r.text


def _crash_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    from assetstudio_server.services import source_publications as sp

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("injected crash")
    monkeypatch.setattr(sp, "publish", boom)


def test_retry_with_fresh_preview_converges_after_crash_between_intent_and_receipt(
        env: PubEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    parts = source_parts("primitive_prop")
    first = env.preview(parts)
    _crash_publish(monkeypatch)
    assert _post(env, first).status_code == 503
    monkeypatch.undo()
    second = env.preview(parts)  # client lost the preview and re-uploaded identical content
    assert second["preview_id"] != first["preview_id"]
    done = env.commit(second, KEY)
    aid, vid = done["asset_ref"]["asset_id"], done["asset_ref"]["version_id"]
    ctx = env.ctx()
    assert len(ctx.store.get(manifest_key(aid), AssetManifest)[0].versions) == 1
    version = ctx.store.get(version_key(aid, vid), AssetVersion)[0]
    assert version.sources["integration"]["preview_id"] == first["preview_id"]  # first attempt stays the provenance
    assert env.commit(first, KEY) == done and env.commit(second, KEY) == done


def test_fresh_preview_after_success_replays_but_changed_content_conflicts(env: PubEnv) -> None:
    parts = source_parts("primitive_prop")
    first = env.preview(parts)
    done = env.commit(first, KEY)
    second = env.preview(parts)
    assert env.commit(second, KEY) == done
    for change in CHANGES:
        _conflict(_post(env, second, **change))


def test_changed_request_with_fresh_preview_conflicts_after_crash(env: PubEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    parts = source_parts("primitive_prop")
    first = env.preview(parts)
    _crash_publish(monkeypatch)
    assert _post(env, first).status_code == 503
    monkeypatch.undo()
    second = env.preview(parts)
    for change in CHANGES:
        _conflict(_post(env, second, **change))
    env.commit(second, KEY)

"""Commit idempotency binds the whole request and the publisher credential; concurrent commits converge."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from assetstudio_core.domain import AssetManifest
from assetstudio_storage.project import manifest_key

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

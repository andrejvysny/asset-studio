"""MCP production tools: Jobs through every gate, Batches/runs, actor, errors, scopes, studio_api (SIMULATED engines)."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from assetstudio_server.services.records import load_decision
from mcp import ClientSession

from tests.conftest import Api
from tests.contract.test_api_batches import approve, confirm_all, create, detail, setup_project
from tests.mcp_support import call, call_error, mcp_app_for, mcp_session

Body = Callable[[ClientSession], Awaitable[None]]


def run_mcp(api: Api, body: Body, scope: str = "full", name: str = "claude") -> None:
    app = mcp_app_for(api.c.app)
    token = app.deps.tokens.create(name, scope)

    async def go() -> None:
        async with mcp_session(app, token) as s:
            await body(s)
    asyncio.run(go())


async def create_item_job(s: ClientSession, pid: str, category: str, key: str, run: bool = True) -> dict[str, Any]:
    return await call(s, "create_job", title=f"Job {key}", category_id=category, candidate_count=2,
                      items=[{"name": f"Thing {key}", "brief": f"a thing {key}"}], run=run,
                      idempotency_key=f"job-{key}-0001", project_id=pid)


async def to_prompts(s: ClientSession, pid: str, job_id: str) -> dict[str, Any]:
    w = await call(s, "wait_for_job", job_id=job_id, until="prompts", timeout_s=30, project_id=pid)
    assert w["done"], w
    return w


async def to_candidates(s: ClientSession, pid: str, job_id: str) -> None:
    await to_prompts(s, pid, job_id)
    await call(s, "confirm_prompts", job_id=job_id, project_id=pid)
    w = await call(s, "wait_for_job", job_id=job_id, until="candidates", timeout_s=30, project_id=pid)
    assert w["done"], w
    item = w["job"]["items"][0]
    assert len(item["candidates"]) == 2 and all(c["qa_status"] for c in item["candidates"])


async def to_published(s: ClientSession, pid: str, job_id: str) -> dict[str, Any]:
    b = await call(s, "build", job_id=job_id, project_id=pid)
    assert b["results"]
    w = await call(s, "wait_for_job", job_id=job_id, until="builds", timeout_s=30, project_id=pid)
    assert w["done"] and w["job"]["items"][0]["build"]["result"] == "valid", w
    acc = await call(s, "accept_build", job_id=job_id, project_id=pid)
    assert acc["job"]["items"][0]["accepted_build"]
    await call(s, "publish", job_id=job_id, project_id=pid)
    w = await call(s, "wait_for_job", job_id=job_id, until="published", timeout_s=30, project_id=pid)
    assert w["done"], w
    return w["job"]


def published_names(api: Api, pid: str) -> list[str]:
    return [a.get("name_id") or a.get("name") for a in api.get(f"/api/v1/projects/{pid}/assets")["items"]]


def test_concept_art_end_to_end_with_actor(api: Api) -> None:
    pid = setup_project(api)
    seen: dict[str, Any] = {}

    async def body(s: ClientSession) -> None:
        out = await create_item_job(s, pid, "concept", "c1")
        assert out["run"]["run_id"] and out["job"]["source"] == "agent:claude"
        jid = out["job"]["id"]
        assert any(j["id"] == jid for j in (await call(s, "list_jobs", project_id=pid))["jobs"])
        await to_candidates(s, pid, jid)
        best = await call(s, "approve_best", job_id=jid, project_id=pid)
        assert best["proposals"] or best["skipped"]
        if not best["proposals"]:  # simulated QA may recommend nothing: decide explicitly
            item = best["job"]["items"][0]
            await call(s, "approve_candidate", job_id=jid, item_id=item["id"], candidate_id=item["candidates"][0]["id"],
                       override_qa=True, override_reason="simulated", idempotency_key="appr-c1-0001", project_id=pid)
        seen["job"] = await to_published(s, pid, jid)
        seen["id"] = jid
    run_mcp(api, body)
    assert published_names(api, pid)
    item = api.get(f"/api/v2/projects/{pid}/jobs/{seen['id']}")["items"][0]
    ctx = api.studio.registry.get(pid)
    assert item["decisions"] and all(load_decision(ctx.store, seen["id"], d).actor == "agent:claude"
                                     for d in item["decisions"])


def test_model3d_override_approval_and_rest_actor_is_operator(api: Api) -> None:
    pid = setup_project(api)
    seen: dict[str, Any] = {}

    async def body(s: ClientSession) -> None:
        jid = (await create_item_job(s, pid, "props", "m1"))["job"]["id"]
        await to_candidates(s, pid, jid)
        item = (await call(s, "get_job", job_id=jid, project_id=pid))["items"][0]
        done = await call(s, "approve_candidate", job_id=jid, item_id=item["id"],
                          candidate_id=item["candidates"][1]["id"], override_qa=True,
                          override_reason="only viable silhouette", idempotency_key="appr-m1-0001", project_id=pid)
        assert done["job"]["items"][0]["approval"]
        job = await to_published(s, pid, jid)
        assert job["items"][0]["build"]["failed_checks"] is None
        seen["id"] = jid
    run_mcp(api, body)
    assert published_names(api, pid)
    ctx = api.studio.registry.get(pid)
    approval = api.get(f"/api/v2/projects/{pid}/jobs/{seen['id']}")["items"][0]["decisions"][0]
    dec = load_decision(ctx.store, seen["id"], approval)
    assert dec.actor == "agent:claude"  # override is stored only when QA actually needed it
    intent = api.studio.journal.tasks.intent(pid, "approve", "appr-m1-0001")
    assert intent is not None and intent["intent"]["plan"]["actor"] == "agent:claude"
    # the same gate over plain REST is the operator
    bid = create(api, pid, ["Rest thing"], "rest-actor-1")["batch"]["id"]
    api.wait_ops()
    confirm_all(api, pid, bid, "rest-confirm-1")
    api.wait_ops()
    it = detail(api, pid, bid)["items"][0]
    assert approve(api, pid, bid, it, 0, "rest-approve-1")["results"][0]["ok"]
    rest_item = detail(api, pid, bid)["items"][0]
    assert load_decision(ctx.store, bid, rest_item["approval"]).actor == "operator"


def test_gate_errors_nothing_ready_and_stale(api: Api) -> None:
    pid = setup_project(api)

    async def body(s: ClientSession) -> None:
        jid = (await create_item_job(s, pid, "concept", "e1", run=False))["job"]["id"]
        assert "no items are ready" in await call_error(s, "confirm_prompts", job_id=jid, project_id=pid)
        await call(s, "run_job", job_id=jid, project_id=pid)
        await to_prompts(s, pid, jid)
        item = (await call(s, "get_job", job_id=jid, project_id=pid))["items"][0]
        old_prompt = item["prompt"]["id"]
        await call(s, "edit_prompt", job_id=jid, item_id=item["id"], description="a new idea", project_id=pid)
        err = await call_error(s, "confirm_prompts", job_id=jid, item_ids=[item["id"]],
                               prompt_revision_id=old_prompt, project_id=pid)
        assert "409" in err or "stale" in err, err
        await call(s, "confirm_prompts", job_id=jid, project_id=pid)
        await call(s, "wait_for_job", job_id=jid, until="candidates", timeout_s=30, project_id=pid)
        item = (await call(s, "get_job", job_id=jid, project_id=pid))["items"][0]
        await call(s, "regenerate", job_id=jid, item_ids=[item["id"]], project_id=pid)
        await call(s, "wait_for_job", job_id=jid, until="candidates", timeout_s=30, project_id=pid)
        missing = await call_error(s, "approve_candidate", job_id=jid, item_id=item["id"],
                                   candidate_id="cnd_missing", project_id=pid)
        assert "not found" in missing
    run_mcp(api, body)


def test_read_token_reads_but_gates_are_forbidden(api: Api) -> None:
    pid = setup_project(api)
    app, app2 = mcp_app_for(api.c.app), mcp_app_for(api.c.app)  # a session manager runs once per app
    full, read = app.deps.tokens.create("claude", "full"), app.deps.tokens.create("viewer", "read")

    async def go() -> None:
        async with mcp_session(app, full) as s:
            jid = (await create_item_job(s, pid, "concept", "r1"))["job"]["id"]
            await to_prompts(s, pid, jid)
        async with mcp_session(app2, read) as s:
            assert (await call(s, "get_job", job_id=jid, project_id=pid))["id"] == jid
            assert "forbidden" in await call_error(s, "confirm_prompts", job_id=jid, project_id=pid)
            assert "forbidden" in await call_error(s, "studio_api", method="POST", path="/api/v1/projects")
    asyncio.run(go())


def test_batch_run_gates_across_jobs(api: Api) -> None:
    pid = setup_project(api)

    async def body(s: ClientSession) -> None:
        jobs = [(await create_item_job(s, pid, "concept", f"b{i}", run=False))["job"]["id"] for i in (1, 2)]
        batch = await call(s, "create_batch", name="Pair", job_ids=jobs, idempotency_key="batch-pair-0001",
                           project_id=pid)
        bid = batch["batch"]["id"]
        started = await call(s, "start_batch", batch_id=bid, project_id=pid)
        rid = started["run_id"]
        assert started["plan"]["counts"]["jobs"] == 2
        for j in jobs:
            await to_prompts(s, pid, j)
        assert (await call(s, "get_batch", batch_id=bid, project_id=pid))["job_ids"] == jobs
        wave = await call(s, "run_gate", run_id=rid, gate="confirm", project_id=pid)
        assert len(wave["results"]) == 2 and all(r["ok"] for r in wave["results"])
        for j in jobs:
            w = await call(s, "wait_for_job", job_id=j, until="candidates", timeout_s=30, project_id=pid)
            assert w["done"], w
        best = await call(s, "run_gate", run_id=rid, gate="approve_best", project_id=pid)
        assert len(best["proposals"]) + len(best["skipped"]) == 2
        run = await call(s, "get_run", run_id=rid, project_id=pid)
        assert len(run["jobs"]) == 2
        if best["proposals"]:
            assert run["jobs"][0]["counts"]["approved"] + run["jobs"][1]["counts"]["approved"] == len(
                best["proposals"])
        assert "no items are ready" in await call_error(s, "run_gate", run_id=rid, gate="accept", project_id=pid)
        assert (await call(s, "run_control", run_id=rid, action="pause", project_id=pid))["action"] == "pause"
        assert (await call(s, "run_control", run_id=rid, action="resume", project_id=pid))["action"] == "resume"
        assert (await call(s, "list_batches", project_id=pid))["batches"]
    run_mcp(api, body)


def test_wait_times_out_without_error_and_studio_api_guards(api: Api) -> None:
    pid = setup_project(api)

    async def body(s: ClientSession) -> None:
        jid = (await create_item_job(s, pid, "concept", "w1", run=False))["job"]["id"]
        w = await call(s, "wait_for_job", job_id=jid, until="prompts", timeout_s=1, project_id=pid)
        assert w["done"] is False and "call wait_for_job again" in w["reason"]
        projects = await call(s, "studio_api", method="GET", path="/api/v1/projects")
        assert projects["projects"][0]["id"] == pid
        for bad in ("/files/x", "/api/../x", "/api/v3/x", "/api/v1/projects?x=1"):
            assert "refused" in await call_error(s, "studio_api", method="GET", path=bad)
        assert (await call(s, "list_tasks", project_id=pid))["tasks"] is not None
        assert "passes" in await call(s, "list_passes")
        assert "backend" in await call(s, "storage_status", project_id=pid)
    run_mcp(api, body)

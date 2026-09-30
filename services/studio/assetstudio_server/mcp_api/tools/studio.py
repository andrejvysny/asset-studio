"""Studio-wide tools: overview, projects, recipes, runtime."""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..annotations import READ, WRITE
from ..deps import Deps


def _readiness(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r[k]["state"] for k in ("generation", "build", "qa") if isinstance(r.get(k), dict)}


def register(mcp: FastMCP, deps: Deps) -> None:
    @mcp.tool(annotations=READ)
    async def studio_overview(ctx: Context) -> dict[str, Any]:
        """Start here. Projects, engine mode (simulated or real), service health, and recipe readiness.

        A recipe is usable for generation only when its `generation` state is `ready` (or `experimental`)."""
        c = deps.client(ctx)
        projects = await c.get("/api/v1/projects")
        rt = await c.get("/api/v1/runtime")
        return {
            "projects": projects["projects"],
            "simulated": rt["simulated"], "engine_mode": rt["engine_mode"],
            "services": [{k: s.get(k) for k in ("name", "role", "reachable", "ready", "problems")}
                         for s in rt["services"]],
            "gpus": rt["gpus"],
            "models_not_ready": sorted(m["key"] for m in rt["models"] if m["status"] != "ready"),
            "recipes": [{"id": r["id"], "kind": r["kind"], "label": r["label"], **_readiness(r)}
                        for r in rt["recipes"]],
        }

    @mcp.tool(annotations=WRITE)
    async def create_project(ctx: Context, name: str, root: str | None = None,
                             starter_qa: bool = True) -> dict[str, Any]:
        """Create an empty project. `root` must lie under a configured project root (default: first root/<slug>).
        `starter_qa` adds a starter QA ruleset. Define categories next (config_set section='categories')."""
        c = deps.client(ctx, write=True)
        return await c.post("/api/v1/projects", {"name": name, "root": root, "starter_qa": starter_qa})

    @mcp.tool(annotations=WRITE)
    async def register_project(ctx: Context, root: str) -> dict[str, Any]:
        """Open an existing project folder (under a configured project root) in this Studio."""
        c = deps.client(ctx, write=True)
        return await c.post("/api/v1/projects:register", {"root": root})

    @mcp.tool(annotations=READ)
    async def project_summary(ctx: Context, project_id: str | None = None) -> dict[str, Any]:
        """Counts (assets, planned, jobs, batches, categories, media) and work waiting at each human gate.
        `project_id` may be omitted when exactly one project is open."""
        c = deps.client(ctx)
        pid = await deps.project_id(c, project_id)
        return await c.get(f"/api/v1/projects/{pid}/summary")

    @mcp.tool(annotations=READ)
    async def list_recipes(ctx: Context) -> dict[str, Any]:
        """Built-in recipes (one per kind): stages, typed parameters (key, type, default, min/max, choices),
        prompt template and readiness. Parameters are set per project via config_set section='pipelines'."""
        c = deps.client(ctx)
        return await c.get("/api/v1/capabilities")

    @mcp.tool(annotations=READ)
    async def list_loras(ctx: Context) -> dict[str, Any]:
        """Locally registered LoRAs: speed LoRAs and style LoRAs usable as `style_lora` in category defaults."""
        c = deps.client(ctx)
        return await c.get("/api/v1/loras")

    @mcp.tool(annotations=READ)
    async def runtime_status(ctx: Context, include_models: bool = False) -> dict[str, Any]:
        """GPUs, services, lanes and recent model passes. `include_models` adds per-model file/licence status."""
        c = deps.client(ctx)
        rt = await c.get("/api/v1/runtime")
        rt.pop("recipes", None)
        if not include_models:
            rt.pop("models", None)
            rt.pop("licences", None)
        return rt

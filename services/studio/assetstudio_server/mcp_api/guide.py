"""Agent-facing orientation: server instructions, reference resources and the produce_asset prompt."""
from __future__ import annotations

import json

from assetstudio_core.config import StudioConfig
from mcp.server.fastmcp import FastMCP

from .deps import Deps

INSTRUCTIONS = """AssetStudio: self-hosted production and library of digital assets (3D models, sprites, icons,
materials, concept art, sprite sheets, VFX flipbooks). Call studio_overview first.

Model: a project has a revisioned configuration (categories with inherited defaults, styles, pipelines = recipe
parameters, QA rulesets, reference sets, export presets). A Job produces one asset per item through gates:
brief -> enhanced prompt (confirm_prompts) -> candidates + advisory QA (approve_candidate / approve_best)
-> build (build) -> accept (accept_build) -> publish (publish). Generation and builds run asynchronously on GPUs:
use wait_for_job between gates. Every decision you make is recorded with your token name as the actor.

Mutations accept an optional idempotency_key: pass the same key when retrying after a timeout. Errors read
`<status> <code>: <message>`; 409 means state changed (re-read, then retry). Read assetstudio://guide/workflow
for the full sequence and assetstudio://schema/config for the configuration schema."""

WORKFLOW = """# Producing an asset
1. studio_overview: check the recipe for your kind is `ready` (generation + build).
2. config_get: categories, styles, pipelines. Add or adjust with config_set (read-modify-write, revision-checked).
3. create_job(title, category_id, items=[{name, brief}], run=true): saving alone never starts inference.
4. wait_for_job(until='prompts') -> inspect items[].prompt -> optionally edit_prompt -> confirm_prompts.
5. wait_for_job(until='candidates') -> inspect candidates (get_artifact for images) and QA ->
   approve_candidate (override_qa + reason for not-recommended ones) or approve_best, or regenerate.
6. build -> wait_for_job(until='builds') -> inspect the build (validation, preview) -> accept_build.
7. publish -> the asset appears in search_assets; get_download_url for files.
Several Jobs: create_batch + start_batch, then run_gate across Jobs of the run.
Variants of a published asset: variant_capabilities -> variant_draft -> create_variant_jobs.

# Configuration
Fields in `defaults` and in each category's `defaults` are overrides: {"mode": "inherit"} | {"mode": "value",
"value": ...} | {"mode": "disabled"}. Resolution: recipe -> project defaults -> parent categories -> category.
Changes affect new Jobs only (existing Jobs keep their snapshot)."""

PRODUCE_ASSET = """Produce a {kind} asset named "{name}" in project {project_id}. Brief: {brief}

Follow assetstudio://guide/workflow: check readiness, pick or create a fitting category, create the Job with
run=true, then pass each gate with wait_for_job in between. Inspect candidates before approving; explain any QA
override. Finish by publishing and report the asset id and version."""


def register(mcp: FastMCP, deps: Deps) -> None:
    @mcp.resource("assetstudio://guide/workflow", mime_type="text/markdown")
    def workflow() -> str:
        """Gate sequence and configuration semantics."""
        return WORKFLOW

    @mcp.resource("assetstudio://schema/config", mime_type="application/json")
    def config_schema() -> str:
        """JSON Schema of a project's studio.yaml (StudioConfig)."""
        return json.dumps(StudioConfig.model_json_schema())

    @mcp.prompt()
    def produce_asset(project_id: str, kind: str, name: str, brief: str) -> str:
        """Walk an agent through producing and publishing one asset."""
        return PRODUCE_ASSET.format(project_id=project_id, kind=kind, name=name, brief=brief)

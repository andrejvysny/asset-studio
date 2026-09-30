"""Project configuration tools: read, edit one entry at a time (revision-checked), validate, replace."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ..annotations import DESTRUCTIVE, READ, WRITE
from ..client import StudioClient
from ..deps import Deps

Section = Literal["defaults", "categories", "pipelines", "qa_rulesets", "styles", "reference_sets",
                  "export_presets", "retention", "project"]
EditSection = Literal["defaults", "categories", "pipelines", "qa_rulesets", "styles", "reference_sets",
                      "export_presets", "retention"]
DictSection = Literal["pipelines", "qa_rulesets", "styles", "reference_sets", "export_presets", "categories"]
KEYED = ("pipelines", "qa_rulesets", "styles", "reference_sets", "export_presets", "categories")
SINGLETON = ("defaults", "retention")

SECTIONS_DOC = """Sections: defaults (project-wide category defaults), categories (list; addressed by id),
pipelines (per-recipe parameters), qa_rulesets, styles, reference_sets, export_presets (all keyed by id), retention.
Fields under defaults and under each category's defaults are Overrides: {"mode": "inherit"} |
{"mode": "value", "value": ...} | {"mode": "disabled"}; resolution is recipe -> project defaults -> parent
categories -> category. Full JSON Schema: resource assetstudio://schema/config."""


def with_sections(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Appends the shared section/override reference to a tool docstring (the agent sees it in the tool list)."""
    fn.__doc__ = f"{(fn.__doc__ or '').rstrip()}\n\n{SECTIONS_DOC}"
    return fn


def _find_category(config: dict[str, Any], key: str) -> int | None:
    return next((i for i, c in enumerate(config["categories"]) if c.get("id") == key), None)


def _current(config: dict[str, Any], section: str, key: str | None) -> Any:
    if section == "categories":
        i = _find_category(config, key or "")
        return None if i is None else config["categories"][i]
    return config[section] if key is None else config[section].get(key)


def _check_target(section: str, key: str | None) -> None:
    if section in KEYED and not key:
        raise ToolError(f"invalid_request: section {section!r} requires `key` (the entry id)")
    if section in SINGLETON and key is not None:
        raise ToolError(f"invalid_request: section {section!r} takes no `key`; its value replaces the section")


def _apply(config: dict[str, Any], section: str, key: str | None, value: Any, merge: bool) -> Any:
    _check_target(section, key)
    if not isinstance(value, dict):
        raise ToolError("invalid_request: `value` must be an object")
    if merge:
        base = _current(config, section, key)
        if not isinstance(base, dict):
            raise ToolError(f"invalid_request: merge=true but {section}{'.' + key if key else ''} does not exist")
        value = {**base, **value}
    if section == "categories":
        if value.get("id", key) != key:
            raise ToolError(f"invalid_request: value.id {value['id']!r} must equal key {key!r}")
        value = {**value, "id": key}
        i = _find_category(config, key or "")
        if i is None:
            config["categories"].append(value)
        else:
            config["categories"][i] = value
    elif key is None:
        config[section] = value
    else:
        config[section][key] = value
    return value


async def _read_config(c: StudioClient, pid: str, expected_revision: int | None) -> dict[str, Any]:
    view = await c.get(f"/api/v1/projects/{pid}/config")
    if expected_revision is not None and expected_revision != view["revision"]:
        raise ToolError(f"stale_config: expected revision {expected_revision} but the configuration is at "
                        f"revision {view['revision']}; config_get and re-apply")
    return view


async def _save(c: StudioClient, pid: str, revision: int, config: dict[str, Any]) -> dict[str, Any]:
    return await c.patch(f"/api/v1/projects/{pid}/config", {"expected_revision": revision, "config": config})


def register(mcp: FastMCP, deps: Deps) -> None:
    @mcp.tool(annotations=READ)
    @with_sections
    async def config_get(ctx: Context, project_id: str | None = None, section: Section | None = None,
                         include_yaml: bool = False) -> dict[str, Any]:
        """Read the project configuration and its revision (pass the revision as expected_revision when editing).
        Without `section` returns the whole config; with it, just that section as `value`. Always includes the
        category tree. `include_yaml` adds the raw studio.yaml text.
        """
        c = deps.client(ctx)
        pid = await deps.project_id(c, project_id)
        view = await c.get(f"/api/v1/projects/{pid}/config")
        out: dict[str, Any] = {"revision": view["revision"], "categories": view["categories"]}
        if section is None:
            out["config"] = view["config"]
        else:
            out.update(section=section, value=view["config"][section])
        if include_yaml:
            out["yaml"] = view["yaml"]
        return out

    @mcp.tool(annotations=READ)
    async def config_effective(ctx: Context, category_id: str, project_id: str | None = None) -> dict[str, Any]:
        """Resolved settings of one category after inheritance: per field {value, mode, source} where `source`
        names where the value came from (recipe, project, a parent category or the category itself), plus example
        file names. Use it to check what a new Job in this category would get."""
        c = deps.client(ctx)
        pid = await deps.project_id(c, project_id)
        effective = (await c.get(f"/api/v1/projects/{pid}/config"))["effective"]
        if category_id not in effective:
            raise ToolError(f"unknown category {category_id!r}; categories: {', '.join(sorted(effective)) or 'none'}")
        return {"category_id": category_id, "effective": effective[category_id]}

    @mcp.tool(annotations=WRITE)
    @with_sections
    async def config_set(ctx: Context, section: EditSection, value: dict[str, Any], key: str | None = None,
                         merge: bool = False, expected_revision: int | None = None,
                         project_id: str | None = None) -> dict[str, Any]:
        """Create or update one configuration entry (read-modify-write, revision-checked; never retried).
        pipelines/qa_rulesets/styles/reference_sets/export_presets: `key` is the entry id, `value` its object.
        categories: `key` is the category id (created if new, otherwise replaced in place). defaults/retention:
        no `key`; `value` replaces the section. `merge=true` shallow-merges `value` into the existing entry
        (top-level keys only) instead of replacing it. `expected_revision` (from config_get) makes the edit fail
        with stale_config when someone else changed the config meanwhile. Invalid values fail with
        invalid_config and field paths; nothing is saved. Changes affect new Jobs only.
        """
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        view = await _read_config(c, pid, expected_revision)
        config = view["config"]
        _apply(config, section, key, value, merge)
        out = await _save(c, pid, view["revision"], config)
        return {"revision": out["revision"], "section": section, "key": key,
                "value": _current(out["config"], section, key)}

    @mcp.tool(annotations=DESTRUCTIVE)
    async def config_delete(ctx: Context, section: DictSection, key: str, expected_revision: int | None = None,
                            project_id: str | None = None) -> dict[str, Any]:
        """Delete one entry from pipelines, qa_rulesets, styles, reference_sets, export_presets or categories.
        Deleting a category that assets or planned shots still use fails with category_in_use (archive or
        reclassify first). Existing Jobs keep their config snapshot."""
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        view = await _read_config(c, pid, expected_revision)
        config = view["config"]
        if _current(config, section, key) is None:
            raise ToolError(f"unknown {section} entry {key!r}")
        if section == "categories":
            config["categories"].pop(_find_category(config, key))  # type: ignore[arg-type]
        else:
            del config[section][key]
        out = await _save(c, pid, view["revision"], config)
        return {"revision": out["revision"], "section": section, "deleted": key}

    @mcp.tool(annotations=READ)
    @with_sections
    async def config_validate(ctx: Context, yaml: str | None = None, config: dict[str, Any] | None = None,
                              project_id: str | None = None) -> dict[str, Any]:
        """Dry-run validation of a whole configuration without saving. Give exactly one of `yaml` (studio.yaml
        text) or `config` (the whole config object). Returns {ok, errors: [{path, message}], config}.
        """
        c = deps.client(ctx)
        pid = await deps.project_id(c, project_id)
        if (yaml is None) == (config is None):
            raise ToolError("invalid_request: give exactly one of yaml / config")
        return await c.post(f"/api/v1/projects/{pid}/config:validate", {"yaml": yaml, "config": config})

    @mcp.tool(annotations=DESTRUCTIVE)
    @with_sections
    async def config_replace_yaml(ctx: Context, yaml: str, expected_revision: int,
                                  project_id: str | None = None) -> dict[str, Any]:
        """Replace the WHOLE configuration with studio.yaml text. Anything not in the text is removed; prefer
        config_set for single entries. `expected_revision` is required (stale_config on mismatch); the new
        revision is assigned by the server. Validate first with config_validate.
        """
        c = deps.client(ctx, write=True)
        pid = await deps.project_id(c, project_id)
        out = await c.patch(f"/api/v1/projects/{pid}/config", {"expected_revision": expected_revision, "yaml": yaml})
        return {"revision": out["revision"], "config": out["config"]}

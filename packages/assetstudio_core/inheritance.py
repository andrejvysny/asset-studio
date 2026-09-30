"""Semantic config validation + effective-value resolution with sources.

Resolution: recipe defaults -> project defaults -> category ancestors (root first) -> category -> item overrides.
Scalars and lists replace whole; `disabled` resolves to None deliberately.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .canonical import sha256_json
from .config import DEFAULT_FIELDS, CategoryDefaults, FieldError, Mode, StudioConfig
from .kinds import Kind
from .naming import validate_template
from .recipes import DEFAULT_RECIPE_FOR_KIND, RECIPES, Recipe, validate_parameters

ITEM_OVERRIDABLE = ("kind", "candidate_count")


class Resolved(BaseModel):
    value: Any = None
    mode: Mode = Mode.inherit
    source: str = "unset"


def category_chain(cfg: StudioConfig, category_id: str | None) -> list[str]:
    """Root-first ancestor chain ending at category_id. Assumes a validated (acyclic) config."""
    chain: list[str] = []
    cur = cfg.category(category_id) if category_id else None
    while cur is not None and cur.id not in chain:
        chain.insert(0, cur.id)
        cur = cfg.category(cur.parent_id) if cur.parent_id else None
    return chain


def descendants(cfg: StudioConfig, category_id: str) -> set[str]:
    out = {category_id}
    changed = True
    while changed:
        changed = False
        for c in cfg.categories:
            if c.parent_id in out and c.id not in out:
                out.add(c.id)
                changed = True
    return out


def validate_semantics(cfg: StudioConfig) -> list[FieldError]:
    errs: list[FieldError] = []
    ids = [c.id for c in cfg.categories]
    for cid in {i for i in ids if ids.count(i) > 1}:
        errs.append(FieldError(path=f"categories.{cid}", message="duplicate category id"))
    by_id = {c.id: c for c in cfg.categories}
    siblings: dict[tuple[str | None, str], str] = {}
    for i, c in enumerate(cfg.categories):
        path = f"categories.{i}"
        if c.parent_id is not None and c.parent_id not in by_id:
            errs.append(FieldError(path=f"{path}.parent_id", message=f"unknown parent {c.parent_id!r}"))
        key = (c.parent_id, c.slug)
        if key in siblings and siblings[key] != c.id:
            errs.append(FieldError(path=f"{path}.slug", message=f"duplicate sibling slug {c.slug!r}"))
        siblings[key] = c.id
        seen, cur = {c.id}, c.parent_id
        while cur is not None and cur in by_id:
            if cur in seen:
                errs.append(FieldError(path=f"{path}.parent_id", message="category cycle"))
                break
            seen.add(cur)
            cur = by_id[cur].parent_id
        errs += _validate_defaults(cfg, c.defaults, f"{path}.defaults")
    errs += _validate_defaults(cfg, cfg.defaults, "defaults")
    for rid, ps in cfg.pipelines.items():
        recipe = RECIPES.get(rid)
        if recipe is None:
            errs.append(FieldError(path=f"pipelines.{rid}", message="unknown recipe"))
            continue
        for key, msg in validate_parameters(recipe, ps.parameters):
            errs.append(FieldError(path=f"pipelines.{rid}.parameters.{key}", message=msg))
    for sid, rs in cfg.qa_rulesets.items():
        for j, rule in enumerate(rs.rules):
            if rule.metric == "palette_reserved" and not any(s.palette for s in cfg.styles.values()):
                errs.append(FieldError(path=f"qa_rulesets.{sid}.rules.{j}", message="palette rule but no palette"))
    for pid, preset in cfg.export_presets.items():
        if (msg := validate_template(preset.path_pattern, path=True)) is not None:
            errs.append(FieldError(path=f"export_presets.{pid}.path_pattern", message=msg))
    return errs


def _validate_defaults(cfg: StudioConfig, d: CategoryDefaults, path: str) -> list[FieldError]:
    errs: list[FieldError] = []
    refs = {"qa_ruleset": cfg.qa_rulesets, "reference_set": cfg.reference_sets, "style": cfg.styles}
    for name, table in refs.items():
        ov = getattr(d, name)
        if ov.mode == Mode.value and ov.value not in table:
            errs.append(FieldError(path=f"{path}.{name}", message=f"unknown {name} {ov.value!r}"))
    if d.recipe_id.mode == Mode.value and d.recipe_id.value not in RECIPES:
        errs.append(FieldError(path=f"{path}.recipe_id", message=f"unknown recipe {d.recipe_id.value!r}"))
    if d.export_presets.mode == Mode.value:
        for p in d.export_presets.value or []:
            if p not in cfg.export_presets:
                errs.append(FieldError(path=f"{path}.export_presets", message=f"unknown export preset {p!r}"))
    if d.naming.mode == Mode.value and (msg := validate_template(d.naming.value or "")) is not None:
        errs.append(FieldError(path=f"{path}.naming", message=msg))
    if d.candidate_count.mode == Mode.value and not 1 <= (d.candidate_count.value or 0) <= 8:
        errs.append(FieldError(path=f"{path}.candidate_count", message="must be 1–8"))
    return errs


def _layers(cfg: StudioConfig, category_id: str | None) -> list[tuple[str, CategoryDefaults]]:
    layers = [("project", cfg.defaults)]
    for cid in category_chain(cfg, category_id):
        c = cfg.category(cid)
        assert c is not None
        layers.append((f"category:{cid}", c.defaults))
    return layers


def _apply(out: dict[str, Resolved], source: str, d: CategoryDefaults, fields: tuple[str, ...]) -> None:
    for name in fields:
        ov = getattr(d, name)
        if ov.mode == Mode.value:
            value = ov.value.model_dump() if isinstance(ov.value, BaseModel) else ov.value
            out[name] = Resolved(value=value, mode=Mode.value, source=source)
        elif ov.mode == Mode.disabled:
            out[name] = Resolved(value=None, mode=Mode.disabled, source=source)


def resolve(cfg: StudioConfig, category_id: str | None, item: dict[str, Any] | None = None) -> dict[str, Resolved]:
    out: dict[str, Resolved] = {f: Resolved() for f in DEFAULT_FIELDS}
    for source, layer in _layers(cfg, category_id):
        _apply(out, source, layer, DEFAULT_FIELDS)
    for key, value in (item or {}).items():
        if key in ITEM_OVERRIDABLE and value is not None:
            out[key] = Resolved(value=value, mode=Mode.value, source="item")
    kind = out["kind"].value
    if out["recipe_id"].mode == Mode.inherit and kind is not None:
        out["recipe_id"] = Resolved(value=DEFAULT_RECIPE_FOR_KIND[Kind(kind)], mode=Mode.value, source="recipe")
    recipe = RECIPES.get(out["recipe_id"].value or "")
    if recipe is not None:
        if out["naming"].mode == Mode.inherit:
            out["naming"] = Resolved(value=recipe.default_naming, mode=Mode.value, source="recipe")
        if out["qa_ruleset"].mode == Mode.inherit and recipe.kind.value in cfg.qa_rulesets:
            # Convention: a rule set keyed by the kind is that kind's project-wide default.
            out["qa_ruleset"] = Resolved(value=recipe.kind.value, mode=Mode.value, source=f"qa:{recipe.kind.value}")
        if out["candidate_count"].mode == Mode.inherit:
            pipe = cfg.pipelines.get(recipe.id)
            spec = recipe.param("candidate_count")
            if pipe is not None and "candidate_count" in pipe.parameters:
                out["candidate_count"] = Resolved(value=pipe.parameters["candidate_count"], mode=Mode.value,
                                                  source=f"pipeline:{recipe.id}")
            elif spec is not None:
                out["candidate_count"] = Resolved(value=spec.default, mode=Mode.value, source="recipe")
    return out


class ResolutionError(ValueError):
    pass


def effective_recipe(resolved: dict[str, Resolved]) -> Recipe:
    kind, rid = resolved["kind"].value, resolved["recipe_id"].value
    if kind is None:
        raise ResolutionError("no asset kind: set one on the category or the item")
    recipe = RECIPES.get(rid or "")
    if recipe is None:
        raise ResolutionError(f"unknown recipe {rid!r}")
    if recipe.kind != Kind(kind):
        raise ResolutionError(f"recipe {recipe.id} produces {recipe.kind.value}, not {kind}")
    return recipe


def build_snapshot(cfg: StudioConfig, category_id: str | None, item: dict[str, Any] | None = None) -> dict[str, Any]:
    """Complete effective configuration for one batch item, including referenced contents. Immutable once hashed."""
    resolved = resolve(cfg, category_id, item)
    recipe = effective_recipe(resolved)
    values = {k: r.value for k, r in resolved.items()}
    pipe = cfg.pipelines.get(recipe.id)
    params = {**recipe.default_params(), **(pipe.parameters if pipe else {})}
    param_sources = {k: (f"pipeline:{recipe.id}" if pipe and k in pipe.parameters else "recipe") for k in params}
    if values["candidate_count"] is not None:
        params["candidate_count"] = values["candidate_count"]
        param_sources["candidate_count"] = resolved["candidate_count"].source

    def ref(table: dict[str, Any], key: str | None) -> Any:
        return table[key].model_dump(mode="json") if key and key in table else None

    snap = {
        "schema_version": 1,
        "reference_routing": 1,  # project reference sets are routed to their consumers (absent = older snapshot)
        "project_id": cfg.project.id,
        "config_revision": cfg.revision,
        "category_id": category_id,
        "category_chain": category_chain(cfg, category_id),
        "values": values,
        "sources": {k: r.source for k, r in resolved.items()},
        "recipe": {"id": recipe.id, "version": recipe.version, "kind": recipe.kind.value},
        "parameters": params,
        "parameter_sources": param_sources,
        "template": (pipe.template if pipe and pipe.template is not None else recipe.template),
        "negative": recipe.negative,
        "qa_ruleset": ref(cfg.qa_rulesets, values["qa_ruleset"]),
        "style": ref(cfg.styles, values["style"]),
        "reference_set": ref(cfg.reference_sets, values["reference_set"]),
    }
    snap["sha256"] = sha256_json(snap)
    return snap


def name_parts(cfg: StudioConfig, category_id: str | None) -> tuple[str, str]:
    """(top-level category slug, leaf slug when nested) for the {category} and {sub} naming variables."""
    chain = category_chain(cfg, category_id)
    if not chain:
        return "", ""
    root, leaf = cfg.category(chain[0]), cfg.category(chain[-1])
    assert root is not None and leaf is not None
    return root.slug, leaf.slug if len(chain) > 1 else ""

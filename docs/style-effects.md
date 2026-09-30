# Style and configuration effects

This file lists every field a Job item freezes in its configuration snapshot. For each field it gives the code that
reads it and the effect that field has. The live report is `GET /api/v1/projects/{p}/config:effects` for a new Job and
`GET /api/v2/projects/{p}/jobs/{j}/items/{i}/effects` for an existing item. MCP clients use the `config_effects` tool.
Both reports come from `packages/assetstudio_core/effects.py`, and the table below must match that file.

The classifications describe the **planned mechanism**. Only execution receipts, QA results and build checks show what
actually ran.

| Classification | Meaning |
|---|---|
| `applied` | A registered operation or check implements the field directly. |
| `conditioning_only` | The field is sent to a generative model, which is not guaranteed to comply. |
| `advisory_only` | The field informs review or QA and never blocks. |
| `not_applicable` | The field does not apply to this route. An example is style negatives in source-conditioned edits. |
| `unsupported` | Nothing reads the field. A set value produces a non-fatal config `warning`. |

## Style profile (`styles.<id>`, assigned through the `style` default)

| Field | Consumer | Classification | Notes |
|---|---|---|---|
| `guide` | prompt enhancer, `coordinator/stages/prompt.py` | conditioning_only | The enhancer writes the description. The guide text is not pasted into the image prompt. |
| `guide` | variant compare QA `variant_style`, `qa_compare.py` | advisory_only | Generative variants only. |
| `negative` | image-model negative prompt, `promptrev.edit_texts` | conditioning_only (T2I) / not_applicable (edit) | Edit mode uses the fixed `EDIT_NEGATIVE`. |
| `palette[reserved]` | candidate QA `palette_reserved`, `stages/qa.py` | applied if an enabled rule exists, else not_applicable | The kind and category allow-lists apply. |
| `palette[unreserved]` | none | advisory_only | Reference information only. |

History: each distinct content becomes one immutable `styles/<id>/<sha>.json` record. The `sha` equals
`PromptRevision.style_sha`.

## Reference sets (`reference_sets.<id>`, assigned through the `reference_set` default)

| Mode | Consumer | Classification |
|---|---|---|
| `prompt_guidance` | enhancer, after item references, within the 4-image limit; a variant source uses one slot | conditioning_only |
| `qa_reference` | compare QA, one check per image, after item references, max 4 | advisory_only |
| `image_conditioning` | none; generation fails with 422 `reference_conditioning_unavailable` | unsupported |

The selection code is `services/reference_bindings.py`. Images that are not selected appear in `references_excluded`
with a reason. Snapshots without `reference_routing` were created before routing existed. Their set is never routed
and is reported as `unsupported`.

## Category defaults

| Field | Consumer | Classification |
|---|---|---|
| `kind`, `recipe_id` | stage plan / recipe | applied |
| `naming` | publication file names | applied |
| `qa_ruleset` | candidate QA | applied |
| `candidate_count` | image generation | applied (a recipe parameter) |
| `budget.triangles` | 3D export decimation target + advisory `triangle_budget` check | applied (model3d), not_applicable (others) |
| `budget.size_px`, `budget.frames` | none | unsupported |
| `build_profile` | see "Build profiles (model3d)" below | applied (model3d), not_applicable (others) |
| `export_presets` | none; delivery exporters are planned | unsupported |
| `style_lora` | none; generation fails with 422 `style_lora_unavailable` | unsupported |

## Build profiles (model3d)

`build_profiles.<id>` is a typed table; `build_profile` (a category default) references one by id, and unknown ids are
rejected. Snapshots embed the resolved profile. Unset fields keep the exporter default.

- Geometry fields are sent to the 3D worker export (`coordinator/builds/model3d.py::geometry_policy`). Studio refuses
  to send them to a worker that does not advertise `geometry_policy.v1` (`worker_feature_missing`). The policy
  applies to the non-remesh path only.
- Material fields run in the CPU material stage (`coordinator/builds/material.py`,
  `assetstudio_processing/materials.py`). The stage runs after the bake and before final sizing, and it proves with
  `preservation_checks` that only material fields and the rewritten texture view changed. The pre-policy GLB is
  kept as the intermediate `model_unmaterialized` and is never published.
- A rebuild (`mode: rebuild`) accepts the same keys as overrides. A material-only rebuild reuses the bake and makes no
  3D worker call; a geometry override re-exports from the stored raw.

| Field | Consumer | Notes |
|---|---|---|
| `geometry.small_components` (`remove`/`preserve`) | 3D worker export cleanup | default remove |
| `geometry.fill_holes` (`upstream`/`disabled`) | 3D worker export cleanup | TRELLIS decoding also fills holes before the raw is stored; that step is not controlled |
| `geometry.expect_single_component` | build check `single_component` | unset = advisory as today |
| `material.alpha_mode` (`opaque`/`mask`/`blend`/`auto`) | CPU material stage (GLB rewrite) | `auto`: MASK when >1% texels are below the cutoff, else OPAQUE |
| `material.alpha_cutoff` | CPU material stage (GLB rewrite) | only with `mask`/`auto` |
| `material.double_sided` | CPU material stage (GLB rewrite) | |
| `material.metallic` | CPU material stage (GLB rewrite) | replaces metallic texture + factor |
| `material.roughness_min`, `roughness_max` | CPU material stage (GLB rewrite) | clamps the packed roughness texture (linear), not a factor; min <= max |

## Recipe parameters

Every recipe parameter is `applied` by its recipe's stages. Source-conditioned variant edits use the plan's edit
parameters. For those edits, `steps`, `cfg`, `width`, `height` and `speed_preset` are `not_applicable`.

## Known gaps (not implemented)

These gaps come from the spec "Configurable Game Styles and 3D Production", Phase 2 and later:
- Hole filling during TRELLIS decoding (before the raw is stored) is not controlled; `fill_holes: disabled` only
  affects the exporter.
- Build profiles apply to the whole asset; per-part material bindings (wood staves vs iron hoops) do not exist yet.
- There are no typed geometry cleanup, normal or material-channel policies, no source-image reconstruction fast path,
  and no runtime validation in Godot.

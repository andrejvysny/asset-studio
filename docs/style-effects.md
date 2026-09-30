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
| `build_profile` | none; 3D export uses the recipe parameters | unsupported |
| `export_presets` | none; delivery exporters are planned | unsupported |
| `style_lora` | none; generation fails with 422 `style_lora_unavailable` | unsupported |

## Recipe parameters

Every recipe parameter is `applied` by its recipe's stages. Source-conditioned variant edits use the plan's edit
parameters. For those edits, `steps`, `cfg`, `width`, `height` and `speed_preset` are `not_applicable`.

## Known gaps (not implemented)

These gaps come from the spec "Configurable Game Styles and 3D Production", Phase 2 and later:
- The 3D export always writes an `OPAQUE` material, and `doubleSided` follows `remesh`. Foliage cutouts therefore need
  explicit alpha-mode and culling policies.
- There are no typed geometry cleanup, normal or material-channel policies, no source-image reconstruction fast path,
  and no runtime validation in Godot.

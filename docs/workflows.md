# Workflows, Asset Studio and App Mode

**Asset Studio** (http://127.0.0.1:8190) is the day-to-day UI. **ComfyUI** (:8188) stays the execution engine and
the v1 API: the Studio creates and advances jobs only by queuing the workflows below through the ComfyUI `/prompt`
API (proxied at `/comfy/*`). The App Mode apps in ComfyUI remain available as a fallback.

## Flow: two human gates
| Step | Studio screen | Workflow (config/workflows) | Job state after |
|---|---|---|---|
| 1 brief + enhance | New job | `line_a_enhance` | `prompt_enhanced` (no images) |
| 2 edit + confirm, generate + QA | New job (step 2) | `line_a_generate` | `waiting_for_selection` |
| 3 review, approve one candidate | Review | `POST /line_a/jobs/{id}/approve` then `line_a_3d` | `completed` / `failed_*` |
| re-export from raw (no resample) | 3D attempts | `line_a_reexport` | new attempt |
| assign to catalog slot | 3D attempts / Asset detail | `PUT /api/slots/{slot}/assignment` | library/assignments.json |

- **Prompt**: the user edits the *description* only; `effective = description + model-sheet template`
  (`config/prompts/model_sheet_template.txt`) is always applied. Stored: `enhanced-prompt.original.txt`,
  `enhanced-prompt.edited.txt`, `enhanced-prompt.final.txt` (effective).
- **Approval binding**: generation freezes `candidates/set.json` (`set_id` + sha256 per image). Approval must name
  `{set_id, index, image_sha256}`; stale set / changed image / busy job -> 409. Repeating an identical approval returns
  the existing attempt (no second 3D run).
- **QA** is advisory: `recommended` (all configured checks ran and passed), `not_recommended` (major fail or >= 2 minor
  fails), `unverified` (some checks did not run, e.g. a service was down). Any candidate can be approved.
- **3D attempts** live in `output/<job>/model/attempts/att-NN/` (selected.png, cutout, `raw/raw.pt`, processed GLB,
  `validation.json`, logs). Attempts are immutable; a new approval or re-export creates a new one. `completed` requires
  GLB validation (reload, non-empty finite geometry, valid indices, UVs, embedded texture).
- **Cleanup** is conservative by default: remesh off, floater removal off (and computed on a position-welded copy so UV
  seams are never mistaken for floaters). Upstream `to_glb` always fills holes < 0.03 perimeter (recorded per attempt).

- **QA override**: approving a `not_recommended` / `unverified` / unchecked candidate needs an explicit override
  (Studio checkbox "Override QA", App Mode "Approve anyway", CLI `approve --override`, API `override: true`).
  It is recorded as `qa_override` on the attempt and in the manifest. QA never blocks.

## Asset library
- Catalog: `config/catalog.json` v2 (Asset Library Kit, 7 biomes x 7 layers) with **explicit, stable ids** for every
  layer, family and slot, e.g. `forest_prop_containers_storage_a`. Ids never change once assigned (renaming a family keeps
  them). After adding families or raising a `count`, run `make catalog-ids`; `make catalog-check` fails if ids are missing.
  The service refuses to start on duplicate/malformed ids or `count` != number of slot ids.
- Library filter "Assigned only" (`?show=assigned`) hides planned slots.
- Only completed + validated attempts can be assigned; an attempt occupies one slot (reassigning moves it).
- Runtime licence inventory: `config/licences.yaml` (shown in Studio > Runtime and as a banner when anything is
  `not_cleared`). Currently nvdiffrast v0.4.0 (texture bake) is non-commercial.

## Remote access
Both UIs bind to 127.0.0.1 on the host:
```
ssh -N -L 8190:127.0.0.1:8190 -L 8188:127.0.0.1:8188 <user>@<host>      # add -J <user>@<gateway> if needed
```
then open http://localhost:8190 (Studio) or http://localhost:8188 (ComfyUI).

## CLI (same workflows)
```
python3 scripts/run_job.py new "wooden medieval barrel" --asset-type small_prop --triangles 500
python3 scripts/run_job.py confirm <job-id> [--edited-file desc.txt] [--speed lightning_8step]
python3 scripts/run_job.py approve <job-id> <index>
python3 scripts/run_job.py reexport <job-id> <att-NN> [--remesh] [--drop-floaters]
```

## Regenerating workflows
Edit `scripts/export-workflows.py`, then `python3 scripts/export-workflows.py` with ComfyUI running (widget order is
read from `/object_info`). Writes `config/workflows/*.api.json`, App Mode `*.app.json`, and copies the apps into
`comfyui/user/default/workflows/`.

## Web development
No Node on the host is required: `podman run --rm -v "$PWD/web":/web:Z -w /web docker.io/library/node:22-slim npx tsc -b --noEmit`.
The library image builds the web bundle (`npm ci` from `web/package-lock.json`); rebuild it after web changes:
`podman build -f services/library/Dockerfile -t localhost/line-a-library:dev .`

## End-to-end tests (Playwright)
```
make e2e       # builds web, runs tests/e2e: isolated library UI tests (fixture data) + live-stack screens
make e2e-gpu   # full job flow on the real stack: enhance -> edit -> generate -> review -> (override) -> approve -> 3D
```
Screenshots land in `tests/e2e/artifacts/`. Every test fails on any browser console error or page exception.

# TODO — Line A

Plan: ~/.claude/plans/act-as-software-developer-spicy-cupcake.md (Asset Studio + review fixes)
Branch: feat/asset-studio

## Phase 1 — pipeline contract fixes (review R02–R06, R10, R11)
- [x] job_io: per-job flock, state validation before side effects, active_operation, root containment, can_transition guard
- [x] attempts: model/attempts/att-NN + attempt.json (replaces timestamp archive)
- [x] R03 enhance / confirm split: ConfirmPrompt effective = edit + template; workflows line_a_enhance, line_a_generate, line_a_3d
- [x] R04 candidates/set.json (sha256), POST /line_a/jobs/{id}/approve, explicit job id in App Mode, drop run_job --select
- [x] R05 qa_rules: recommended / not_recommended / unverified + coverage; UNVERIFIED tile
- [x] R02/R10 worker: raw.pt before export, cleanup flags off by default, /reexport, GLB validation, mesh_info requested vs effective
- [x] R11 lora_strength None check; routes path normalise + strict index; seed via secrets
- [x] unit tests for all of the above

## Phase 2 — services/library
- [x] config/catalog.json, catalog.py (slot naming = design slug)
- [x] assignments.py (library/assignments.json)
- [x] index.py (jobs, attempts, unassigned results)
- [x] API + /comfy proxy (HTTP + WS), /api/runtime, config/licences.yaml
- [x] compose service :8190, Dockerfile (multi-stage web build)
- [x] tests

## Phase 3 — web/ Asset Studio (Vite + React + TS)
- [x] scaffold, theme tokens, shell/nav
- [x] Library + Coverage + AssetDetail
- [x] Jobs + NewJob (2 steps)
- [x] Review (grid/focus/matrix + approve bar)
- [x] Attempts (model-viewer, validation, re-export, assign)
- [x] Runtime
- [x] headless e2e + screenshots

## Done since
- [x] catalog.json v2: explicit stable layer/family/slot ids (scripts/catalog-ids.py, make catalog-check); validated at load
- [x] Library "Assigned only" filter (?show=assigned, survives reload/biome switch, empty state)
- [x] QA override: non-recommended/unverified/unchecked approvable only with explicit override; recorded (attempt + manifest + run.log); all-failed banner
- [x] Playwright e2e (tests/e2e, make e2e / make e2e-gpu): 6 UI tests on isolated fixture server + live screens + GPU flow; screenshots tests/e2e/artifacts/
- [x] fixes from e2e: approve bar showed "anyway" while QA running; legacy QA coverage 0/14; attempt texture/file/sha formatting; locale numbers
- [x] optimisations: model-viewer/three.js lazy-loaded (initial JS 1.39 MB -> 369 KB); catalog cache invalidated on file mtime; worker fails fast naming missing weights

## Untested until DINOv3 access (GPU path)
- [ ] raw.pt save/load + /reexport on a real TRELLIS mesh
- [ ] GLB validation + attempt completion on real export; model-viewer on real GLB
- [ ] assign real validated attempt to slot (store unit-tested only)
- [ ] worker meshcheck tests inside pinned trellis image (host trimesh 4.9.0 run passes)

## Next work packages
- [ ] R01 P0 DECIDED: replace trellis-worker runtime with a 2nd headless ComfyUI ("comfy-3d", GPU1) running native TRELLIS.2
      (comfy/ldm/trellis2 + nodes_mesh_postprocess: pure PyTorch, no nvdiffrast/cumesh/flex_gemm/flash-attn; weights
      Comfy-Org/TRELLIS.2 @ pinned rev). Keep bridge contract (attempt dirs, raw save, GLB validation). Spike first:
      quality vs current, VRAM on 24 GB, runtime licence audit (ComfyUI GPL-3.0 internal use), DINOv3 licence.
- [ ] R07 GPU1 ownership (partial: unload fail-closed on timeout/HTTP error; still missing active-use counting, op ids, cancellation)
- [ ] R08 Docker-first compose, Podman override
- [ ] R09 verifier anchored to config (library models_check does it; scripts/verify-models.py still manifest-trusting); R12 locks/docs/offline test
- [ ] amend SPEC §32.4 (Studio primary UI, ComfyUI engine + v1 API) once SPEC.md is in repo
- [ ] make lock-comfyui (constraints.txt still empty)
- [ ] acceptance run once DINOv3 granted

## Open
- DINOv3: accept Meta licence on HF (needed whichever copy is used; Comfy-Org repackage is ungated but same licence terms)
- decided: explicit catalog ids (user); npm (not bun); port 8190; exporter = native ComfyUI TRELLIS.2 (spike)

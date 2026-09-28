# TODO — Line A v1

Plan: ~/.claude/plans/act-as-software-developer-spicy-cupcake.md

## Phase 1 — skeleton + deploy
- [x] dir layout
- [x] .gitignore, .env.example, Makefile
- [x] config/app.yaml, config/models.yaml (pinned SHAs)
- [x] scripts/download-models.sh + download_models.py + verify-models.py
- [x] scripts/install-host.sh (checks only)
- [x] comfyui Dockerfile (v0.37.4) + extra_model_paths.yaml
- [x] prompt-service Dockerfile + skeleton app
- [x] trellis-worker Dockerfile (TRELLIS.2 75fbf01, CUDA exts) + skeleton app
- [x] compose.yml (CDI GPU pin, internal net)
- [x] smoke-test.sh scaffold
- [x] build all images on podman host (o-voxel needed submodules)
- [x] download models (all but dinov3: gated, access pending)
- [ ] make lock-comfyui after first build; rebuild
- [x] compose up on podman-compose 1.0.6 (no depends_on; comfy writable dirs)
- [ ] smoke-test.sh run

## Phase 2 — job_io + prompt enhancement
- [x] job_io lib + 17 unit tests
- [x] prompt templates, QA rules.yaml
- [x] /enhance impl + EnhancePrompt node
## Phase 3 — candidate generation node/workflow (+ LoRA, Lightning preset)
- [x] GenerateCandidates + OptionalLora + workflows + scripts/run_job.py
- [x] Lightning preset (8-step: ~10s/variant, more floor/shadow artifacts)
- [ ] test style LoRA
## Phase 4 — QA (BiRefNet heuristics + VLM)
- [x] impl, 22 unit tests
- [x] QA flags floor/shadow on lightning outputs (not just lenient)
- [ ] test w/ deliberately bad prompts (2 objects, cropped)
## Phase 5 — selection routes + JS panel
- [x] routes + select node (re-select archives to attempts/)
- [x] review UI = ComfyUI App Mode apps (New Asset, Review & Approve), verified headless
- [x] scripts/export-workflows.py generates api+app workflows from /object_info
- [ ] 3D viewer output untested until dinov3
## Phase 6 — cutout + TRELLIS worker
- [x] cutout verified e2e
- [ ] TRELLIS run blocked on dinov3 access; failure path verified
## Phase 7 — post-process + manifest
- [x] code written (untested until dinov3)
- [ ] torch cu130 for ComfyUI optimized ops?
## Phase 8 — tests + docs

## Open
- HF token w/ DINOv3 license accepted?
- triangle floor (2k proposed)
- branch name
- SPEC.md: drop spec into repo root

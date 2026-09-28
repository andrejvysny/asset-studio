# Workflows & review UI

Two ComfyUI **App Mode** apps (simple form + outputs, no node graph). Open ComfyUI →
Workflows sidebar → pick the app. It opens in App Mode.

| App | Does | Output |
|---|---|---|
| `Line A - 1 New Asset` | prompt → enhance → N variants → QA | QA overview grid, per-variant tiles, summary (≡) |
| `Line A - 2 Review & Approve` | review a job; approving a variant runs cut-out → TRELLIS.2 → GLB | same gallery; after approval: 3D viewer (GLB), preview, mesh info |

## Typical flow
1. **New Asset**: type prompt, pick asset type → **Run**.
   `Speed preset`: `quality` (50 steps, ~2 min/variant) or `lightning_8step`/`lightning_4step` (~10 s/variant, more
   floor/shadow artifacts).
2. Review the grid: green = recommended, orange = not recommended + reasons. Advisory only.
   The enhanced prompt is in the summary (≡ icon, last thumbnail).
3. Want a different prompt? Paste an edited version into `Edited prompt` → **Run** (creates a new job).
4. **Review & Approve**: `Job` = `latest` (or a job id) → **Run** with `review only` to re-show the gallery.
5. Set `Approve variant` to e.g. `02` → **Run**. Generates the 3D model; result shows in the 3D viewer.
   Approving again with another variant archives the previous attempt to `output/<job>/attempts/`.

Everything is written to `output/<job-id>/` (see SPEC §15). Gallery images shown in the UI are copies in
ComfyUI's temp dir; the job dir is the source of truth.

## Remote access
ComfyUI binds to `127.0.0.1:8188` on the host. From your machine:
```
ssh -N -L 8188:127.0.0.1:8188 <user>@<host>                 # direct
ssh -N -J <user>@<gateway> -L 8188:127.0.0.1:8188 <user>@<host>  # via gateway
```
then open http://localhost:8188.

## Editing the apps
Apps are generated, not hand-edited: change `scripts/export-workflows.py`, then
`python3 scripts/export-workflows.py` (needs running ComfyUI; reads `/object_info` for widget order).
Writes `config/workflows/*.api.json` (automation, `scripts/run_job.py`) and `*.app.json` (UI) +
copies into `comfyui/user/default/workflows/`.

## Automation (ComfyUI API)
```
./scripts/run_job.py new "wooden medieval barrel" --asset-type small_prop --triangles 500 [--speed lightning_8step]
./scripts/run_job.py select <job-id> <index>
```

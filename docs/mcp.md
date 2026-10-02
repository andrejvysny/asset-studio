# MCP server (remote AI agents)

AssetStudio serves a [Model Context Protocol](https://modelcontextprotocol.io) endpoint, so an AI agent (Claude Code,
Claude Desktop/SDK, Cursor, any MCP client) can do everything the UI does:
- configure a project: categories, defaults, styles, pipelines, QA, references, export, retention;
- create and run Jobs and Batches and pass every gate;
- import files, manage media, the shot list and the library;
- create variants and inspect the runtime.

Design: `docs/architecture.md` § Security posture.

## Endpoint

| | |
|---|---|
| Transport | Streamable HTTP (stateless), `POST /mcp` |
| Listener | `STUDIO_MCP_HOST:STUDIO_MCP_PORT`, default `127.0.0.1:8191` (Docker: published on `MCP_BIND:MCP_PORT`) |
| Auth | `Authorization: Bearer ast_…` |
| Disable | `STUDIO_MCP=0` |

The UI/REST port (8190) has **no authentication** and must stay on loopback. Expose only the MCP port. Put a TLS
reverse proxy or tunnel in front of it (nginx, Tailscale, cloudflared, ...), then set
`STUDIO_MCP_PUBLIC_URL=https://…`. That URL is the only non-loopback `Host` the endpoint accepts (DNS-rebinding
protection), and it is the base of signed file URLs.

## Tokens

```sh
assetstudio mcp create claude                 # full scope; the token is printed once
assetstudio mcp create observer --scope read  # read-only tools
assetstudio mcp list
assetstudio mcp revoke claude                 # immediate, also while the Studio runs
# Docker: docker compose exec studio assetstudio mcp create claude
```

- Tokens are stored as sha256 hashes in `<instance>/mcp_tokens.json` (mode 0600).
- A `full` token may pass **every human gate**: confirm prompts, approve candidates, accept builds, publish.
- Decisions record `actor: agent:<token name>`. Jobs created by an agent record `source: agent:<name>`.

## Connecting a client

```sh
# Claude Code
claude mcp add --transport http assetstudio https://studio.example.net/mcp \
  --header "Authorization: Bearer ast_…"
```

```json
// generic JSON client config (Cursor, Claude Desktop via mcp-remote, …)
{"mcpServers": {"assetstudio": {"url": "https://studio.example.net/mcp",
                                "headers": {"Authorization": "Bearer ast_…"}}}}
```

Python SDK: `streamable_http_client(url, http_client=httpx.AsyncClient(headers={"Authorization": "Bearer …"}))`.

## Tools

`ᴿ` = read-only (allowed for `read` tokens). `project_id` may be omitted when exactly one project is open.
In proxy auth mode (`STUDIO_AUTH_MODE=proxy`) the operator-administration tools `register_project`, `config_set`,
`config_delete`, `config_replace_yaml`, `reset_lane` and `rebuild_index` are not listed: agents get 403 for them there.

| Group | Tools |
|---|---|
| Studio | studio_overviewᴿ, create_project, register_project, project_summaryᴿ, list_recipesᴿ, list_lorasᴿ, runtime_statusᴿ |
| Config | config_getᴿ, config_effectiveᴿ, config_effectsᴿ, style_historyᴿ, config_set, config_delete, config_validateᴿ, config_replace_yaml |
| Library | list_categoriesᴿ, search_assetsᴿ, get_assetᴿ, update_asset, set_current_version, list_familiesᴿ, update_family, get_artifactᴿ, get_download_urlᴿ |
| Files, media, import | upload_file, create_upload_url, search_mediaᴿ, add_media, update_media, archive_media, import_asset, shot_list_getᴿ, shot_list_put, shot_list_import |
| Jobs | create_job, list_jobsᴿ, get_jobᴿ, run_job, cancel_job, item_reference, set_item_preset, wait_for_jobᴿ |
| Gates | enhance_prompts, edit_prompt, confirm_prompts, regenerate, approve_candidate, approve_best, clear_approval, build, reexport, retry_preview, run_transform, accept_build, publish |
| Batches, runs | create_batch, update_batch, list_batchesᴿ, get_batchᴿ, start_batch, get_runᴿ, run_gate, run_control |
| Variants | variant_capabilitiesᴿ, variant_draft, variant_draft_action, create_variant_jobs, compare_variants, get_diversityᴿ |
| Ops | list_tasksᴿ, get_taskᴿ, task_control, list_passesᴿ, reset_lane, rebuild_index, storage_statusᴿ, studio_api |

Resources: `assetstudio://guide/workflow` (gate sequence, config semantics) and `assetstudio://schema/config` (JSON
Schema of `studio.yaml`). Prompt: `produce_asset(project_id, kind, name, brief)`.

### Conventions

- **Gates bind automatically.** If `item_ids` is omitted, the tool selects every item ready for that gate. Omitted ids
  (prompt revision, candidate set, approval, build) come from the item's current state together with its revision.
  If the item changed in between, the Studio still refuses with `409 stale_item`. Explicit ids are passed through as
  given.
- **Asynchronous work** (enhance, generate + QA, build, publish): call `wait_for_job(job_id, until=…)`. It caps at
  120 s. `done: false` means call it again, not an error.
- **Retries**: every mutation takes an optional `idempotency_key`. Reuse the same key when retrying after a
  timeout. Errors read `<status> <code>: <message>` plus `detail`. On `409`, re-read and retry.
- **Configuration**: `config_set` is a revision-checked read-modify-write of one entry (`merge=true` for a shallow
  update). Changes affect new Jobs only.
- **Files**:
  - Upload: `upload_file` (base64, ≤ 16 MiB) or `create_upload_url` (one-time `PUT` of raw bytes, 15 min, up to
    512 MiB). Both return an `upload_id` for `add_media`, `import_asset` or `item_reference`.
  - `get_artifact(include_image=true)` returns a downscaled image for review.
  - `get_download_url` returns a signed `GET` URL (15 min, Range support).
  - The Studio never downloads URLs you pass it.
- `studio_api(method, path, body, query)` reaches any `/api/v1|v2` route that has no dedicated tool.

## Typical session

```
studio_overview → config_get → config_set(section="styles", key="lowpoly", value={...})
create_job(title, category_id, items=[{name, brief}], run=true)
wait_for_job(until="prompts") → confirm_prompts
wait_for_job(until="candidates") → get_artifact(include_image=true) → approve_candidate | approve_best
build → wait_for_job(until="builds") → accept_build → publish → search_assets → get_download_url
```

## Tests

- `tests/contract/test_mcp_*.py` run the full listener stack in-process with SIMULATED engines: bearer auth, then
  Streamable HTTP, then tools, then the REST loopback.
- They cover auth and scopes, the actor on decisions and command intents, config editing, file spool and signed
  URLs, and end-to-end concept-art and 3D runs up to publication.

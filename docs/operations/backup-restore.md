# Backup and restore

Two independent backups. Take both with **Studio stopped** (no instance-level lock exists; the project backup
enforces its own writer lock, the instance backup relies on you stopping the process).

| | Command | Covers |
|---|---|---|
| Project | `assetstudio project backup PROJECT [--out DIR]` / `project restore-verify FILE` | project root (blobs, metadata) + journal copy |
| Instance | `assetstudio instance backup [--out DIR]` / `instance restore-verify FILE` | integration `server.json` (server identity), integration `tokens.json`, `mcp_tokens.json`, `projects.json` (project registry: ids and absolute roots), journal snapshot, `auth.sqlite` snapshot (node mode: runner credentials, registration tokens, offer-signing keys) |

Default output is `<instance>/backups`. The instance archive is `instance-<UTC timestamp>.tar.gz` (mode 0600) with a
`backup_manifest.json` (`assetstudio-instance-backup/1`: `server_id` + sha256/size per member). Token stores hold
SHA-256 digests only; plaintext tokens are never stored and cannot be recovered. The journal is captured through
SQLite's online-backup API (consistent snapshot, never a raw file copy). Per-process secrets (MCP signed-URL key, actor
secret) are deliberately not backed up.

`restore-verify` checks tar member safety, every hash/size, that `server.json` holds a canonical UUID equal to the
manifest `server_id`, that token files parse with a `tokens` list, that `projects.json` has a `projects` list, and `PRAGMA integrity_check` on the journal and `auth.sqlite`. Exit 0/1.

## Restore (manual)

1. Stop Studio. Run `restore-verify` on both archives.
2. Restore the instance members to the same relative paths under the (new) instance dir: `integration/server.json`,
   `integration/tokens.json`, `mcp_tokens.json`, `projects.json`, `journal/operations.sqlite`, `auth.sqlite` (keep file mode 0600).
3. Restore projects from their project backups to the roots recorded in `projects.json`. If a root moved, edit its
   `root` in `projects.json` or run `assetstudio project register ROOT` (the project id is kept).
4. Start Studio.

Rules:

- Never let the server generate a new `server_id` after a restore: asset references embed it and clients pin it. If
  `server.json` is missing the server creates a fresh identity, so restore it before the first start. Use
  `assetstudio integration identity adopt SERVER_ID --i-understand-fork` only to move the same server to new storage
  or to fork deliberately.
- Restoring `tokens.json` restores revocations as well: a token revoked after the backup becomes valid again. Re-run
  `assetstudio integration token revoke` / `mcp revoke` for anything revoked since.
- Restoring `auth.sqlite` restores runner credentials and revocations as of the backup: revoke any runner removed
  since. Restored attempts that were live at backup time come back `uncertain`/reconciling; custody repair and runner
  reconciliation settle them, never silent re-execution.
- Runner-local state and spool (node mode) are out of scope: each runner keeps its spool until it receives a
  disposition receipt, so restore Studio first and let runners reconnect.

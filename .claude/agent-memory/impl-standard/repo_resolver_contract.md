---
name: repo-resolver-contract
description: Resolver capability/representations/cache-first facts and fake_server mutate hooks
metadata:
  type: project
---

- Resolver `supported_capabilities` (default Schema.SUPPORTED_CAPABILITIES == capabilities.json known set) is a settable var; tests narrow it since syntax validation already rejects unknown caps.
- Dependency closure is walked by AddCommand.resolve_closure calling prepare per dep; capability check lives in resolver (_fetch_manifest + _from_cache).
- fake_server.py: POST /__mutate (caps/dependency_on, mutate dep first), /__reset_manifests (client_test_base.finish() calls it); scenarios legacy_resolve, rep_unsupported*, rep_missing_key.
- Network _fetch_files trusts has_blob_sized (no rehash); pinned cache-first evicts corrupt blobs to avoid serving them.

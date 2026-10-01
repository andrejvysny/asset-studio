---
name: repo-integration-publication
description: Gotchas for integration publication (preview/commit) code and tests
metadata:
  type: project
---

- Publication code: services/source_publications.py (+ source_publication_checks.py), integration_api/routes_publish.py; test helpers tests/integration_publication_support.py (GLB/draft/multipart builders, PubEnv).
- Adding a schema file to contracts/godot-integration/v1 requires bumping `len(SCHEMA_FILES)` in tests/unit/test_integration_contracts.py (and optionally KINDS for fixture kinds).
- Integration TestClient has raise_server_exceptions=False: injected crashes appear as 503 retryable responses, not raised.
- Tests importing scripts/ modules (integration_fixture_*, make_integration_fixtures): sys.path insert is done in tests/integration_publication_support.py; import lazily inside functions.
- Starlette `request.form(max_fields=0)` rejects non-file parts; uploads need a filename (httpx `files=`).

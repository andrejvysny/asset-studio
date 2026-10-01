---
name: repo-gotchas
description: asset-studio repo facts for implementers (lint scope, pydantic regex, fixtures)
metadata:
  type: project
---

- `ruff check scripts` has a pre-existing E501 in scripts/make_fixture_project.py:102; not a regression.
- pydantic patterns with lookahead need `ConfigDict(regex_engine="python-re")` (default Rust engine rejects them).
- Not a git repo (no git status); check determinism by diffing `shasum` of generated files.
- macOS `sed -i` needs `-i ''`.
- Integration contract fixtures: `uv run python scripts/make_integration_fixtures.py [--check]`; sources in scripts/integration_fixture_*.py.

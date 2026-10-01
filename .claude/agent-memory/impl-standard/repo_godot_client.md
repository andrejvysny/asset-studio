---
name: repo-godot-client
description: Godot addon core (AS-06) gotchas: HTTPRequest limits, test runner, no class_name
metadata:
  type: project
---

- Addon core scripts have no class_name (preload consts only); static `new()` returning RefCounted; ASResult factory is `success()`/`fail()` (`ok` is a member).
- GDScript JSON numbers are all floats; HTTPRequest deletes its download_file on failure and hides status/headers after mid-body errors, and cancel_request() blocks with use_threads: artifact bodies use HTTPClient (as_stream_download.gd).
- Tests: `godot --headless --path integrations/godot --script res://tests/run_tests.gd`; network tests need `python3 integrations/godot/tests/run_client_tests.py` (fake_server.py, scenarios via POST /__scenario); it fails on "SCRIPT ERROR" or token in output since runner prints PASS even when a coroutine errors.
- scripts/package_addon.py -> dist/ (gitignored); version in core/as_version.gd.

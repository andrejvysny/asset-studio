---
name: repo-source-install
description: AS-07b godot_static_source_v1 client install (validator/relocator/receipt) gotchas
metadata:
  type: project
---

- Files: project/as_source_package.gd (validate), as_source_relocator.gd, as_godot_text.gd + as_godot_cursor.gd (parser), as_srcpkg_{zip,manifest,media,rules,policy,install}.gd. Prefix `as_srcpkg_` because the publisher agent owns `as_source_*` names (it overwrote my as_source_policy.gd once).
- Tests: tests/test_source_{package,install,relocator}.gd + source_test_base.gd; run_source_tests.py does fresh-.godot import (probe scripts copied into temp project).
- ZIPReader hides flags/method/attrs: central directory is parsed by hand (as_srcpkg_zip.gd); GDScript has no NFC/casefold, so ASCII-only names + to_lower().
- Inner classes can't call outer-script members on a typed RefCounted var; use untyped `var ck` or `.call()`. BSD sed has no `\|`; edit with python.
- Installer.install(coord, managed_rel, ref, prep, opts) opts = {trust_shaders, closure_keys}; returns entry_rel. Source bindings skip pending_import; finalize refuses non-portable.
- Coordinator crash steps start at 1 (fail_after_step=0 never fires).

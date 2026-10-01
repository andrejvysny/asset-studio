---
name: repo-lifecycle-backup
description: shutdown gate and instance backup facts for asset-studio
metadata:
  type: project
---

- run() uses create_app(owns_studio=False) and closes Studio once in its finally; companions fail together via main._guarded.
- Integration publish threadpool calls go through routes_publish._tracked (MutationGate entered inside the worker thread).
- Instance backup lives in assetstudio_server/instance_backup.py (not backup.py); explicit member list in _token_files; no instance-level lock exists.
- zsh: `sed -i` needs `''` on macOS; tests use IDs from assetstudio_core.ids.new_id("prj").
- Eligibility (modular worktree): services/eligibility.py is the single evaluator; `attempts.acquire` requires `AcquireRequest.free_slots` (empty = no offer), so test acquire helpers must pass slot ids. `offer_call` already places at creation (don't call `place` again in tests). Offers in fence tests may lack `params`/full spec: derive operation from the attempt row.

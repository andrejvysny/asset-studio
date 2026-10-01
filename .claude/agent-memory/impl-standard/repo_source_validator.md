---
name: repo-source-validator
description: Gotchas for source-package validator (assetstudio_processing/source_*.py, godot_text.py)
metadata:
  type: project
---

- zipfile truncates ZipInfo.filename at NUL and caps reads at declared size: validator uses orig_filename and own zlib inflate (source_zip.py) so real-bytes-over-declared is detectable.
- INDEX.json hostile `detail` slugs are the contract (e.g. glb_external_uri, binary_resource for .scn, forbidden_file for project.godot), not the brief's generic ones.
- SourcePackageManifestV1 validator rejects unknown asset_key itself; validator re-derives `unsupported_source_dependency/unknown_asset_key` from raw JSON.

"""Tool modules. Each exposes `register(mcp, deps)`. Read tools use `annotations.READ`; every mutation obtains its
client with `deps.client(ctx, write=True)`, the single scope check."""
from __future__ import annotations

from . import batches, config, jobs, library, media, ops, studio, variants

ALL_MODULES = [studio, config, library, media, jobs, batches, variants, ops]

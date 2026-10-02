"""Prompt variants: one enhanced prompt per preview slot, intentionally different in safe creative dimensions."""
from __future__ import annotations

from typing import Any

from .variant_gen import VariantSource

_KEEP = "Keep the subject, asset type, style and every stated constraint exactly as given."
# Slot 0 is the plain enhancement (no hint); later slots each push one dimension. A hint is a direction for the
# enhancer, never extra requirements: it must not contradict the brief or change what is being made.
VARIATIONS: tuple[str, ...] = (
    "",
    f"Variation focus: composition and viewing angle (a clearly different framing). {_KEEP}",
    f"Variation focus: silhouette and proportions (bulkier, slimmer, taller, squatter). {_KEEP}",
    f"Variation focus: secondary details and surface features (wear, trim, accessories). {_KEEP}",
    f"Variation focus: material and colour emphasis within the stated style. {_KEEP}",
    f"Variation focus: lighting mood and contrast. {_KEEP}",
    f"Variation focus: level of simplification versus ornament. {_KEEP}",
    f"Variation focus: arrangement and pose of the parts. {_KEEP}",
)


def variant_count(snap: dict[str, Any], vs: VariantSource | None) -> int:
    """One prompt per candidate (preview) slot for text-to-image items; source-conditioned edits keep a single
    instruction because the source image, not the prompt, is what varies the result."""
    if vs is not None:
        return 1
    return max(1, min(int(snap.get("parameters", {}).get("candidate_count", 4)), len(VARIATIONS)))


def variation_brief(brief: str, index: int) -> str:
    hint = VARIATIONS[index]
    return f"{brief}\n\n{hint}" if hint else brief

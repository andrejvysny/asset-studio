"""Fixed wording for source-conditioned (image-edit) prompts. The edit model does not reliably state that the input
image is the source, so the sentence is prepended by code, never left to the enhancer."""
from __future__ import annotations

FIXED_SENTENCE = ("Use the input image as the source object: keep its identity unless the change below says "
                  "otherwise.")
EDIT_NEGATIVE = ("collage, comparison panel, multiple objects, duplicate objects, scene, environment, floor, "
                 "text, watermark")
EDIT_TEMPLATES: dict[str, str] = {
    "model3d": ("one complete isolated object, centred with margin, same three-quarter camera view as the source "
                "image, plain light grey background, no floor, no cast shadow"),
    "sprite": "single subject, full figure visible, plain light grey background",
    "icon": "single centred subject, square composition, plain background, no text",
    "concept_art": "keep the source composition and framing",
}


def edit_template(kind: str) -> str:
    return EDIT_TEMPLATES.get(kind, EDIT_TEMPLATES["concept_art"])

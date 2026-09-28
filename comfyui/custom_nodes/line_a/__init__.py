"""Line A bridge nodes: job I/O, prompt bridge, candidate generation, QA, selection, cut-out, TRELLIS."""
from __future__ import annotations

from . import routes  # noqa: F401  (registers /line_a/* HTTP routes)
from .nodes_gen import LineAGenerateCandidates, LineAOptionalLora
from .nodes_job import LineAConfirmPrompt, LineACreateJob, LineAEnhancePrompt
from .nodes_review import LineAApprove, LineAReviewGallery
from .nodes_stages import LineACutout, LineAReexport, LineARunQA, LineATrellis3D

NODE_CLASS_MAPPINGS = {
    "LineACreateJob": LineACreateJob,
    "LineAEnhancePrompt": LineAEnhancePrompt,
    "LineAConfirmPrompt": LineAConfirmPrompt,
    "LineAOptionalLora": LineAOptionalLora,
    "LineAGenerateCandidates": LineAGenerateCandidates,
    "LineARunQA": LineARunQA,
    "LineACutout": LineACutout,
    "LineATrellis3D": LineATrellis3D,
    "LineAReexport": LineAReexport,
    "LineAReviewGallery": LineAReviewGallery,
    "LineAApprove": LineAApprove,
}
NODE_DISPLAY_NAME_MAPPINGS = {k: "Line A: " + k.removeprefix("LineA") for k in NODE_CLASS_MAPPINGS}
WEB_DIRECTORY = "./web"

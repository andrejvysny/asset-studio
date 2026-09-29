"""Stage registry: every schedulable unit of work, its lane, GPU worker and grouping behaviour."""
from __future__ import annotations

from ..builds.common import mark_run
from . import build, diversity, generate, prompt, qa, qa_compare, variant_plan
from .base import Stage, new_task, residency

STAGES: dict[str, Stage] = {s.name: s for s in (
    Stage("enhance", "enhance", "gpu1", "aux", prompt.enhance),
    Stage("generate", "generate", "gpu0", None, generate.generate),
    Stage("mask", "qa", "gpu1", "aux", qa.mask, coalesce=True),
    Stage("qa_vlm", "qa", "gpu1", "aux", qa.qa_vlm, coalesce=True),
    Stage("qa_finalize", "qa", "cpu", None, qa.qa_finalize),
    Stage("segment", "build", "gpu1", "aux", build.segment, on_error=mark_run),
    Stage("sample", "build", "gpu1", "worker3d", build.sample, on_error=mark_run),
    Stage("bake", "build", "gpu1", "worker3d", build.bake, on_error=mark_run),
    Stage("finalize", "build", "cpu", None, build.finalize, on_error=mark_run),
    Stage("derive", "build", "cpu", None, build.derive, on_error=mark_run),
    Stage("preview", "build", "cpu", None, build.preview),
    Stage("publish", "publish", "cpu", None, build.publish_item),
    Stage("variant_analyze", "plan", "gpu1", "aux", variant_plan.analyze),
    Stage("variant_suggest", "plan", "gpu1", "aux", variant_plan.suggest),
    Stage("qa_compare", "qa", "gpu1", "aux", qa_compare.qa_compare, coalesce=True),
    Stage("diversity", "diversity", "gpu1", "aux", diversity.run),
)}

__all__ = ["STAGES", "Stage", "new_task", "residency"]

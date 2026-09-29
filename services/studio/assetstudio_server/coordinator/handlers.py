"""Operation kind -> (handler, item-release hook)."""
from __future__ import annotations

from .tasks_build import build, build_error, publish_error, publish_pass
from .tasks_generate import generate, generate_error
from .tasks_prompt import enhance, enhance_error
from .tasks_qa import qa, qa_error

HANDLERS = {
    "enhance": (enhance, enhance_error),
    "generate": (generate, generate_error),
    "qa": (qa, qa_error),
    "build": (build, build_error),
    "publish": (publish_pass, publish_error),
}

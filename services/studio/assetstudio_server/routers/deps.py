from __future__ import annotations

from fastapi import Request

from ..registry import ProjectContext
from ..studio import Studio


def studio(request: Request) -> Studio:
    return request.app.state.studio


def project(request: Request, project_id: str) -> ProjectContext:
    return request.app.state.studio.registry.get(project_id)

"""Instance configuration (machine-specific; never stored in a portable project)."""
from __future__ import annotations

import os
import secrets
import socket
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser().resolve()


@dataclass
class Settings:
    instance_dir: Path = field(
        default_factory=lambda: _path("STUDIO_INSTANCE_DIR", Path("~/.local/share/assetstudio")))
    config_dir: Path = field(default_factory=lambda: _path("STUDIO_CONFIG_DIR", REPO_ROOT / "config"))
    workflows_dir: Path = field(
        default_factory=lambda: _path("STUDIO_WORKFLOWS_DIR", REPO_ROOT / "comfyui" / "workflows"))
    models_root: Path = field(default_factory=lambda: _path("STUDIO_MODELS_ROOT", REPO_ROOT / "models"))
    web_dir: Path = field(default_factory=lambda: _path("STUDIO_WEB_DIR", REPO_ROOT / "web" / "dist"))
    # Server-side mount roots the operator allows as project locations (never arbitrary browsing).
    project_roots: list[Path] = field(default_factory=lambda: [
        Path(p).expanduser().resolve()
        for p in os.environ.get("STUDIO_PROJECT_ROOTS", "~/AssetStudio").split(":") if p])
    comfy_url: str = field(default_factory=lambda: os.environ.get("COMFY_URL", "http://127.0.0.1:8188"))
    aux_url: str = field(default_factory=lambda: os.environ.get("AUX_URL", "http://127.0.0.1:8001"))
    # "comfyui" (real), "fake" (clearly-labelled simulation for tests/demo) or "none" (library-only).
    engine: str = field(default_factory=lambda: os.environ.get("STUDIO_ENGINE", "comfyui"))
    gpu_ids: dict[str, str] = field(default_factory=lambda: {
        "gpu0": os.environ.get("GPU_IMAGE_ID", "0"), "gpu1": os.environ.get("GPU_AUX_ID", "1")})
    instance_id: str = field(default_factory=lambda: os.environ.get(
        "STUDIO_INSTANCE_ID", f"{socket.gethostname()}-{secrets.token_hex(3)}"))
    start_coordinator: bool = field(default_factory=lambda: os.environ.get("STUDIO_COORDINATOR", "1") == "1")
    max_upload_bytes: int = 512 * 1024 * 1024

    def ensure(self) -> None:
        for sub in ("", "index", "staging", "journal"):
            (self.instance_dir / sub).mkdir(parents=True, exist_ok=True)

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


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _secret(name: str) -> str:
    """`NAME` wins; otherwise `NAME_FILE` (a mounted secret) is read and stripped."""
    value = os.environ.get(name, "")
    path = os.environ.get(f"{name}_FILE", "")
    if not value and path:
        value = Path(path).read_text().strip()
    return value


DEFAULT_ROLE_GROUPS = "owner:assetstudio-owners,reviewer:assetstudio-reviewers,viewer:assetstudio-viewers"
ROLES = ("viewer", "reviewer", "owner")


def parse_role_groups(raw: str) -> dict[str, str]:
    """`role:group,role:group` -> {role: group}. A malformed pair or unknown role refuses startup."""
    out: dict[str, str] = {}
    for pair in filter(None, (p.strip() for p in raw.split(","))):
        role, _, group = pair.partition(":")
        if role not in ROLES or not group:
            raise ValueError(f"STUDIO_ROLE_GROUPS: bad pair {pair!r} (roles: {', '.join(ROLES)})")
        out[role] = group
    return out


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
    worker3d_url: str = field(default_factory=lambda: os.environ.get("WORKER3D_URL", "http://127.0.0.1:8003"))
    # "comfyui" (real), "fake" (clearly-labelled simulation for tests/demo) or "none" (library-only).
    engine: str = field(default_factory=lambda: os.environ.get("STUDIO_ENGINE", "comfyui"))
    execution: str = field(default_factory=lambda: os.environ.get("STUDIO_EXECUTION", "direct"))
    gpu_ids: dict[str, str] = field(default_factory=lambda: {
        "gpu0": os.environ.get("GPU_IMAGE_ID", "0"), "gpu1": os.environ.get("GPU_AUX_ID", "1")})
    instance_id: str = field(default_factory=lambda: os.environ.get(
        "STUDIO_INSTANCE_ID", f"{socket.gethostname()}-{secrets.token_hex(3)}"))
    start_coordinator: bool = field(default_factory=lambda: os.environ.get("STUDIO_COORDINATOR", "1") == "1")
    max_upload_bytes: int = 512 * 1024 * 1024
    # Compute runners (docs/modular/compute-runner.md R3-R6, R14).
    runner_heartbeat_s: int = field(default_factory=lambda: _int("STUDIO_RUNNER_HEARTBEAT_S", 15))
    runner_lease_s: int = field(default_factory=lambda: _int("STUDIO_RUNNER_LEASE_S", 60))
    runner_offer_ttl_s: int = field(default_factory=lambda: _int("STUDIO_RUNNER_OFFER_TTL_S", 30))
    runner_maintenance_s: float = field(
        default_factory=lambda: float(os.environ.get("STUDIO_RUNNER_MAINTENANCE_S", 5.0)))
    # Node mode: upper bound of continuation workers per capability class (image, aux3d).
    node_workers_max: int = field(default_factory=lambda: _int("STUDIO_NODE_WORKERS_MAX", 4))
    runner_token_ttl_s: int = field(default_factory=lambda: _int("STUDIO_RUNNER_TOKEN_TTL_S", 900))
    runner_audience: str = field(default_factory=lambda: os.environ.get(
        "STUDIO_RUNNER_AUDIENCE", os.environ.get("STUDIO_PUBLIC_URL", "http://127.0.0.1:8190")))
    upload_chunk_size: int = field(default_factory=lambda: _int("STUDIO_UPLOAD_CHUNK_SIZE", 8 * 1024 * 1024))
    upload_ttl_s: int = field(default_factory=lambda: _int("STUDIO_UPLOAD_TTL_S", 86400))
    upload_quota_bytes: int = field(default_factory=lambda: _int("STUDIO_UPLOAD_QUOTA_BYTES", 64 * 1024**3))
    upload_runner_quota_bytes: int = field(
        default_factory=lambda: _int("STUDIO_UPLOAD_RUNNER_QUOTA_BYTES", 32 * 1024**3))
    # Concurrent chunk uploads + input downloads; beyond it they get 503 so control routes never starve (R14).
    transfer_concurrency: int = field(default_factory=lambda: _int("STUDIO_TRANSFER_CONCURRENCY", 4))
    disk_floor_bytes: int = field(default_factory=lambda: _int("STUDIO_DISK_FLOOR_BYTES", 2 * 1024**3))
    # Operator identity (docs/modular/compute-runner.md R11, R16): "local" = loopback profile S, the local operator is
    # owner; "proxy" = profile P behind an authenticating reverse proxy, identity headers trusted only with the
    # shared secret.
    auth_mode: str = field(default_factory=lambda: os.environ.get("STUDIO_AUTH_MODE", "local"))
    proxy_secret: str = field(default_factory=lambda: _secret("STUDIO_PROXY_SECRET"))
    proxy_user_header: str = "Remote-User"
    proxy_groups_header: str = "Remote-Groups"
    role_groups: dict[str, str] = field(default_factory=lambda: parse_role_groups(
        os.environ.get("STUDIO_ROLE_GROUPS", DEFAULT_ROLE_GROUPS)))
    # Unauthenticated runner endpoints (register, token/challenge, token): token bucket per client IP (R14).
    ratelimit_per_min: int = field(default_factory=lambda: _int("STUDIO_RATELIMIT_PER_MIN", 10))
    ratelimit_burst: int = field(default_factory=lambda: _int("STUDIO_RATELIMIT_BURST", 5))

    # MCP endpoint for remote agents: its own listener, so only it (bearer tokens) is exposed, never /api or the UI.
    mcp_enabled: bool = field(default_factory=lambda: os.environ.get("STUDIO_MCP", "1") == "1")
    mcp_host: str = field(default_factory=lambda: os.environ.get("STUDIO_MCP_HOST", "127.0.0.1"))
    mcp_port: int = field(default_factory=lambda: int(os.environ.get("STUDIO_MCP_PORT", "8191")))
    # Externally visible base URL (reverse proxy/tunnel): allowed Host header + base of signed file URLs.
    mcp_public_url: str = field(default_factory=lambda: os.environ.get("STUDIO_MCP_PUBLIC_URL", "").rstrip("/"))
    mcp_max_inline_bytes: int = field(
        default_factory=lambda: int(os.environ.get("STUDIO_MCP_MAX_INLINE_BYTES", str(16 * 1024 * 1024))))

    # Godot-integration REST listener (own port; only /api/integration/v1, library-scoped bearer tokens).
    integration_enabled: bool = field(default_factory=lambda: os.environ.get("STUDIO_INTEGRATION_ENABLED", "0") == "1")
    integration_host: str = field(default_factory=lambda: os.environ.get("STUDIO_INTEGRATION_HOST", "127.0.0.1"))
    integration_port: int = field(default_factory=lambda: int(os.environ.get("STUDIO_INTEGRATION_PORT", "8192")))
    integration_allow_insecure_lan: bool = field(
        default_factory=lambda: os.environ.get("STUDIO_INTEGRATION_ALLOW_INSECURE_LAN", "0") == "1")
    integration_tls_cert: str = field(default_factory=lambda: os.environ.get("STUDIO_INTEGRATION_TLS_CERT", ""))
    integration_tls_key: str = field(default_factory=lambda: os.environ.get("STUDIO_INTEGRATION_TLS_KEY", ""))
    # Set only by the Dockerfile: inside a container the bind is 0.0.0.0 and exposure is the compose port mapping.
    integration_container_bind: bool = field(
        default_factory=lambda: os.environ.get("STUDIO_INTEGRATION_CONTAINER_BIND", "0") == "1")
    # Publication admission: staging budget, free-disk floor, heavy-work slots + queue, pending previews per token.
    integration_staging_max_bytes: int = field(
        default_factory=lambda: int(os.environ.get("STUDIO_INTEGRATION_STAGING_MAX_BYTES", str(8 << 30))))
    integration_disk_floor_bytes: int = field(
        default_factory=lambda: int(os.environ.get("STUDIO_INTEGRATION_DISK_FLOOR_BYTES", str(2 << 30))))
    integration_processing_slots: int = field(
        default_factory=lambda: int(os.environ.get("STUDIO_INTEGRATION_PROCESSING_SLOTS", "2")))
    integration_queue_max: int = field(default_factory=lambda: int(os.environ.get("STUDIO_INTEGRATION_QUEUE_MAX", "8")))
    integration_previews_per_token: int = field(
        default_factory=lambda: int(os.environ.get("STUDIO_INTEGRATION_PREVIEWS_PER_TOKEN", "20")))
    contracts_dir: Path = field(default_factory=lambda: _path(
        "STUDIO_CONTRACTS_DIR", REPO_ROOT / "contracts" / "godot-integration" / "v1"))

    def validate(self) -> None:
        if self.auth_mode not in ("local", "proxy"):
            raise ValueError(f"STUDIO_AUTH_MODE must be 'local' or 'proxy', got {self.auth_mode!r}")
        if self.auth_mode == "proxy" and not self.proxy_secret:
            raise ValueError("STUDIO_AUTH_MODE=proxy requires a non-empty STUDIO_PROXY_SECRET (or _FILE)")

    @property
    def integration_dir(self) -> Path:
        return self.instance_dir / "integration"

    @property
    def mcp_base_url(self) -> str:
        host = "127.0.0.1" if self.mcp_host in ("0.0.0.0", "::") else self.mcp_host
        return self.mcp_public_url or f"http://{host}:{self.mcp_port}"

    def ensure(self) -> None:
        for sub in ("", "index", "staging", "journal"):
            (self.instance_dir / sub).mkdir(parents=True, exist_ok=True)

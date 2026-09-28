import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# jobcore (pure job logic) lives inside the ComfyUI node package but has no ComfyUI imports.
sys.path.insert(0, str(ROOT / "comfyui" / "custom_nodes" / "line_a"))
sys.path.insert(0, str(ROOT / "services" / "trellis_worker"))
sys.path.insert(0, str(ROOT / "services" / "library"))

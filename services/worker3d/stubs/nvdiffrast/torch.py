"""Any attribute access fails: this image was built without the research exporter."""
from typing import NoReturn

STUB = True


def __getattr__(name: str) -> NoReturn:
    raise RuntimeError(f"nvdiffrast.{name} is unavailable: this worker was built without the research exporter "
                       "(NVIDIA non-commercial licence); rebuild with RESEARCH_EXPORTER=1 to enable it")

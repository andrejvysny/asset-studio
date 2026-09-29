"""Lazy-load / explicit-unload holder with active-use counting. GPU1 is time-shared: never unload mid-use."""
from __future__ import annotations

import gc
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Generic, TypeVar

import torch

T = TypeVar("T")


class LazyModel(Generic[T]):
    def __init__(self, loader: Callable[[], T], idle_unload_s: float) -> None:
        self._loader = loader
        self._idle_unload_s = idle_unload_s
        self._model: T | None = None
        self._cond = threading.Condition()
        self._active = 0
        self._last_used = 0.0
        self.loads = 0
        threading.Thread(target=self._reaper, daemon=True).start()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @contextmanager
    def use(self) -> Iterator[T]:
        with self._cond:
            if self._model is None:
                self._model = self._loader()
                self.loads += 1
            self._active += 1
            model = self._model
        try:
            yield model
        finally:
            with self._cond:
                self._active -= 1
                self._last_used = time.monotonic()
                self._cond.notify_all()

    def unload(self, timeout: float = 120.0) -> bool:
        """Waits for active users to finish. Returns True only if the weights are actually released."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._active == 0, timeout=timeout):
                return False
            self._model = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        return True

    def _reaper(self) -> None:
        while True:
            time.sleep(15)
            with self._cond:
                idle = self._model is not None and self._active == 0 and \
                    time.monotonic() - self._last_used > self._idle_unload_s
            if idle:
                self.unload(timeout=0)


def gpu_info() -> dict | None:
    """VRAM/utilisation via nvidia-smi: no CUDA context is created just to report status."""
    import subprocess

    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5, check=True)
        name, used, total, util = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
        return {"name": name, "vram_used_mb": int(used), "vram_total_mb": int(total), "util_pct": int(util)}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None

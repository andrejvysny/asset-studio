"""Lazy-load/idle-unload holder. GPU1 is time-shared, so models must not stay resident."""
from __future__ import annotations

import gc
import threading
import time
from typing import Callable, Generic, TypeVar

import torch

T = TypeVar("T")


class LazyModel(Generic[T]):
    def __init__(self, loader: Callable[[], T], idle_unload_s: float) -> None:
        self._loader = loader
        self._idle_unload_s = idle_unload_s
        self._model: T | None = None
        self._lock = threading.RLock()
        self._last_used = 0.0
        threading.Thread(target=self._reaper, daemon=True).start()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def get(self) -> T:
        with self._lock:
            if self._model is None:
                self._model = self._loader()
            self._last_used = time.monotonic()
            return self._model

    def unload(self) -> None:
        with self._lock:
            self._model = None
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    def _reaper(self) -> None:
        while True:
            time.sleep(15)
            with self._lock:
                if self._model is not None and time.monotonic() - self._last_used > self._idle_unload_s:
                    self.unload()

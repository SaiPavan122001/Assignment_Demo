"""One contract for every artifact type: fetch(artifact) -> AdapterResult.

An adapter NEVER raises to its caller and NEVER executes student code. A failed/skipped artifact is a
warning for the pipeline, not a failed evaluation.
"""
import logging
from dataclasses import dataclass, field
from typing import Callable

from app.models import Artifact
from app.storage.base import Storage

log = logging.getLogger("adapters")

OK, PARTIAL, SKIPPED, FAILED, FIXTURE = "ok", "partial", "skipped", "failed", "fixture"


@dataclass
class AdapterResult:
    status: str
    reason: str = ""
    files: dict[str, str] = field(default_factory=dict)  # relative path -> extracted text
    meta: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.status in (OK, PARTIAL, FIXTURE) and bool(self.files)


Adapter = Callable[[Artifact, Storage], AdapterResult]
_REGISTRY: dict[str, Adapter] = {}


def register(artifact_type: str):
    def deco(fn: Adapter) -> Adapter:
        _REGISTRY[artifact_type] = fn
        return fn
    return deco


def fetch(artifact: Artifact, storage: Storage) -> AdapterResult:
    """Dispatch by artifact type; convert any exception into status='failed'."""
    from app.adapters import load_all  # noqa: F401 - ensure adapters are registered
    load_all()
    fn = _REGISTRY.get(artifact.type)
    if fn is None:
        return AdapterResult(FAILED, f"no adapter for type {artifact.type!r}")
    try:
        return fn(artifact, storage)
    except Exception as e:  # noqa: BLE001
        log.exception("adapter crashed", extra={"artifact_id": artifact.id})
        return AdapterResult(FAILED, f"{type(e).__name__}: {str(e)[:200]}")

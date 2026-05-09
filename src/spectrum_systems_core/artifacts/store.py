from __future__ import annotations

from .model import Artifact
from .validation import ensure_valid_status


class ArtifactStore:
    def __init__(self) -> None:
        self._items: dict[str, Artifact] = {}

    def put(self, artifact: Artifact) -> Artifact:
        if artifact.artifact_id in self._items:
            raise ValueError(
                f"artifact_id {artifact.artifact_id} already in store"
            )
        self._items[artifact.artifact_id] = artifact
        return artifact

    def get(self, artifact_id: str) -> Artifact:
        if artifact_id not in self._items:
            raise KeyError(artifact_id)
        return self._items[artifact_id]

    def list(self) -> list[Artifact]:
        return list(self._items.values())

    def update_status(self, artifact_id: str, status: str) -> Artifact:
        ensure_valid_status(status)
        artifact = self.get(artifact_id)
        artifact.status = status
        return artifact

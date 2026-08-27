"""Recoverable mutation snapshots for autonomous dream writes."""

import json
from pathlib import Path
import re
from uuid import uuid4

from dream.memory.artifacts import AtomicArtifactStore
from dream.core.scope import ScopePaths


_SNAPSHOT_ID = re.compile(r"^[a-f0-9]{32}$")


class RollbackService:
    def __init__(self, paths: ScopePaths) -> None:
        self.paths = paths
        self.artifacts = AtomicArtifactStore(paths.agent_root)

    def capture(self, relative_paths: tuple[Path, ...]) -> str:
        snapshot_id = uuid4().hex
        files: list[dict[str, object]] = []
        for relative in relative_paths:
            target = self.artifacts.resolve(relative)
            files.append(
                {
                    "path": relative.as_posix(),
                    "existed": target.exists(),
                    "content": target.read_text(encoding="utf-8") if target.exists() else "",
                }
            )
        manifest = {"snapshot_id": snapshot_id, "files": files}
        self.artifacts.write_text(
            Path("snapshots") / "mutations" / snapshot_id / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return snapshot_id

    def restore(self, snapshot_id: str) -> None:
        if not _SNAPSHOT_ID.fullmatch(snapshot_id):
            raise ValueError("invalid snapshot_id")
        manifest_relative = (
            Path("snapshots") / "mutations" / snapshot_id / "manifest.json"
        )
        raw = self.artifacts.read_text(manifest_relative)
        if not raw:
            raise FileNotFoundError(f"snapshot not found: {snapshot_id}")
        manifest = json.loads(raw)
        if manifest.get("snapshot_id") != snapshot_id:
            raise ValueError("snapshot manifest identity mismatch")
        for item in manifest["files"]:
            relative = Path(str(item["path"]))
            target = self.artifacts.resolve(relative)
            if bool(item["existed"]):
                self.artifacts.write_text(relative, str(item["content"]))
            elif target.exists():
                target.unlink()

"""Atomic, scope-rooted artifact persistence."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import tempfile


@dataclass(frozen=True)
class ArtifactVersion:
    sha256: str
    byte_length: int
    updated_at: str


class AtomicArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def resolve(self, relative: Path) -> Path:
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("artifact path must remain inside its scope")
        return self.root / relative

    def read_text(self, relative: Path) -> str:
        target = self.resolve(relative)
        if not target.exists():
            return ""
        return target.read_text(encoding="utf-8")

    def write_text(self, relative: Path, content: str) -> ArtifactVersion:
        target = self.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = content.encode("utf-8")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=target.parent, prefix=f".{target.name}.", delete=False
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        return ArtifactVersion(
            sha256=hashlib.sha256(encoded).hexdigest(),
            byte_length=len(encoded),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

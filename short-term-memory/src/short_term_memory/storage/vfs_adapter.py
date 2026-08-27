"""User-isolated virtual paths owned by short-term memory."""

from dataclasses import dataclass
from pathlib import Path


def safe_component(value: str, label: str) -> str:
    """Reject values that could escape one virtual directory component."""

    if (
        not value
        or value in {".", ".."}
        or any(character in value for character in ("/", "\\", "\0", "*", "?", "[", "]"))
    ):
        raise ValueError(f"invalid {label}")
    return value


@dataclass(frozen=True)
class UserPaths:
    root: Path
    journals: Path

    def directories(self) -> tuple[Path, ...]:
        return (self.journals,)


class VFSAdapter:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def paths(self, user_id: str) -> UserPaths:
        user_root = self.root / safe_component(user_id, "user_id")
        paths = UserPaths(
            root=user_root,
            journals=user_root / "journals",
        )
        for directory in paths.directories():
            directory.mkdir(parents=True, exist_ok=True)
        return paths

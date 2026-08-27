"""Deterministic UTF-8 JSON export."""

import json
from pathlib import Path
from typing import Any


class JSONExporter:
    def export(self, payload: dict[str, Any], path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        return target

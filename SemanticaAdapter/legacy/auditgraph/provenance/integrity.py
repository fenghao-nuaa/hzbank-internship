"""Deterministic SHA-256 functions for provenance records."""

import hashlib
import json
from dataclasses import asdict
from typing import Any


def canonical_record(entry: Any) -> bytes:
    data = asdict(entry) if not isinstance(entry, dict) else dict(entry)
    data.pop("checksum", None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_checksum(entry: Any) -> str:
    return hashlib.sha256(canonical_record(entry)).hexdigest()


def verify_checksum(entry: Any) -> bool:
    expected = entry.get("checksum") if isinstance(entry, dict) else entry.checksum
    return bool(expected) and compute_checksum(entry) == expected

"""Deterministic normalizers used before extraction."""

import re
import unicodedata

from auditgraph.core.models import SourceDocument


class TextNormalizer:
    def normalize(self, text: str) -> str:
        text = unicodedata.normalize("NFKC", text).replace("\u00a0", " ")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line)

    def normalize_document(self, document: SourceDocument) -> SourceDocument:
        metadata = dict(document.metadata)
        metadata["normalized"] = True
        return SourceDocument(
            source_id=document.source_id,
            source_type=document.source_type,
            content=self.normalize(document.content),
            content_type="text/plain",
            collected_at=document.collected_at,
            metadata=metadata,
        )


class DateNormalizer:
    _CHINESE_DATE = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$")

    def normalize(self, value: str) -> str:
        match = self._CHINESE_DATE.match(value.strip())
        if match:
            year, month, day = (int(part) for part in match.groups())
            return f"{year:04d}-{month:02d}-{day:02d}"
        return value.strip()


class NumberNormalizer:
    _NUMBER = re.compile(r"(-?[\d,.]+(?:\.\d+)?)")

    def normalize(self, value: str) -> int | float:
        match = self._NUMBER.search(unicodedata.normalize("NFKC", value))
        if not match:
            raise ValueError(f"no number found in {value!r}")
        number = float(match.group(1).replace(",", ""))
        if "万" in value:
            number *= 10_000
        elif "亿" in value:
            number *= 100_000_000
        return int(number) if number.is_integer() else number

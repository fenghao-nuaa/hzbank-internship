"""Render one session's cross-day Journal as a stable logical transcript.

Claude source: compact summaries reference the complete transcript. Project
adaptation: the standalone service exposes Journal events through a logical URI.
"""

from dataclasses import dataclass
import json

from short_term_memory.storage.journal_store import JournalStore

JOURNAL_TRANSCRIPT_URI = "journal://current-session"


@dataclass(frozen=True)
class TranscriptLine:
    sequence: int
    text: str


class JournalTranscript:
    def __init__(self, journals: JournalStore) -> None:
        self.journals = journals

    def lines(self, user_id: str, session_id: str) -> tuple[TranscriptLine, ...]:
        events = self.journals.read_original_range(
            user_id, session_id, 1, 2**63 - 1
        )
        return tuple(
            TranscriptLine(
                sequence=event.sequence,
                text=json.dumps(
                    {
                        "sequence": event.sequence,
                        "role": event.role.value,
                        "content": event.content,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            for event in sorted(events, key=lambda item: item.sequence)
        )

    def render(self, user_id: str, session_id: str) -> str:
        return "\n".join(
            f"{line.sequence}\t{line.text}"
            for line in self.lines(user_id, session_id)
        )

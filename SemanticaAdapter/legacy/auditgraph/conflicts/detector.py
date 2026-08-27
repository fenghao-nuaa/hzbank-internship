"""Non-destructive fact conflict detection."""

import hashlib
from collections import defaultdict

from auditgraph.core.models import Conflict, Triplet


class ConflictDetector:
    def detect(self, triplets: list[Triplet]) -> list[Conflict]:
        grouped: dict[tuple[str, str], list[Triplet]] = defaultdict(list)
        for triplet in triplets:
            grouped[(triplet.subject, triplet.predicate)].append(triplet)

        conflicts: list[Conflict] = []
        for (subject, predicate), facts in grouped.items():
            distinct: dict[str, object] = {}
            for fact in facts:
                distinct[repr(fact.object)] = fact.object
            if len(distinct) < 2:
                continue
            digest = hashlib.sha256(f"{subject}|{predicate}".encode()).hexdigest()[:16]
            conflicts.append(
                Conflict(
                    conflict_id=f"conflict:{digest}",
                    kind="value",
                    subject=subject,
                    predicate=predicate,
                    values=list(distinct.values()),
                    source_ids={fact.source_id for fact in facts},
                    severity="high" if len(distinct) > 2 else "medium",
                )
            )
        return conflicts

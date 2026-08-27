"""Run only curators whose Hermes-style interval says they are due."""

from datetime import datetime
from typing import Iterable

from dream.curators.protocol import Curator


class CuratorRegistry:
    def __init__(self, curators: Iterable[Curator]) -> None:
        self.curators = tuple(curators)

    def run_due(self, now: datetime) -> dict[str, object]:
        return {
            curator.name: curator.run()
            for curator in self.curators
            if curator.should_run(now)
        }


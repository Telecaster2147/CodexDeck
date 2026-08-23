"""Bounded counters for observed and unknown upstream protocol families."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

MAX_PROTOCOL_FAMILY_COUNTERS = 128
OTHER_PROTOCOL_FAMILY = "__other__"


@dataclass
class BoundedFamilyCounter:
    """Fixed-cardinality Misra-Gries candidates with an exact total."""

    max_families: int = MAX_PROTOCOL_FAMILY_COUNTERS
    counts: Counter[str] = field(default_factory=Counter)
    total: int = 0
    dropped_family_count: int = 0

    def add(self, family: str) -> None:
        self.total += 1
        if family in self.counts or len(self.counts) < self.max_families:
            self.counts[family] += 1
            return
        self.dropped_family_count += 1
        for candidate in tuple(self.counts):
            self.counts[candidate] -= 1
            if self.counts[candidate] <= 0:
                del self.counts[candidate]

    def snapshot(self) -> Counter[str]:
        result = Counter(self.counts)
        other = self.total - sum(result.values())
        if other:
            result[OTHER_PROTOCOL_FAMILY] = other
        return result

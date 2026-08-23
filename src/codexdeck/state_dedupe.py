"""Bounded duplicate detection for normalized event streams."""

from __future__ import annotations

import hashlib

DEDUPE_FILTER_BITS = 1 << 18
DEDUPE_FILTER_HASHES = 4
DEDUPE_FILTER_DEGRADED_RATIO = 0.90


class BoundedDedupeFilter:
    """Fixed-size duplicate memory with no false negatives for inserted keys."""

    def __init__(
        self,
        bit_count: int = DEDUPE_FILTER_BITS,
        hash_count: int = DEDUPE_FILTER_HASHES,
    ) -> None:
        if bit_count <= 0 or bit_count & (bit_count - 1):
            raise ValueError("bit_count must be a positive power of two")
        self.bit_count = bit_count
        self.hash_count = hash_count
        self.bits = 0

    def _positions(self, value: str) -> tuple[int, ...]:
        digest = hashlib.blake2b(value.encode("utf-8"), digest_size=32).digest()
        mask = self.bit_count - 1
        return tuple(
            int.from_bytes(digest[index * 4 : index * 4 + 4], "little") & mask
            for index in range(self.hash_count)
        )

    def __contains__(self, value: str) -> bool:
        return all(self.bits & (1 << position) for position in self._positions(value))

    def add(self, value: str) -> None:
        for position in self._positions(value):
            self.bits |= 1 << position

    def __len__(self) -> int:
        return self.bits.bit_count()

    @property
    def fill_ratio(self) -> float:
        return len(self) / self.bit_count

    @property
    def degraded(self) -> bool:
        return self.fill_ratio >= DEDUPE_FILTER_DEGRADED_RATIO

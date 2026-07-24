"""Shared identity and content-anchor helpers for append-oriented evidence files."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO, Protocol


STREAM_ANCHOR_BYTES = 64


class AnchoredCursor(Protocol):
    offset: int
    anchor: bytes


def anchor_matches(handle: BinaryIO, cursor: AnchoredCursor) -> bool:
    if not cursor.offset or not cursor.anchor:
        return True
    start = max(0, cursor.offset - len(cursor.anchor))
    handle.seek(start)
    return handle.read(cursor.offset - start) == cursor.anchor


def refresh_anchor(path: Path, cursor: AnchoredCursor) -> None:
    try:
        with path.open("rb") as handle:
            start = max(0, cursor.offset - STREAM_ANCHOR_BYTES)
            handle.seek(start)
            cursor.anchor = handle.read(cursor.offset - start)
    except OSError:
        cursor.anchor = b""


def anchor_sha256(anchor: bytes) -> str:
    return hashlib.sha256(anchor).hexdigest() if anchor else ""


def stream_source_id(
    prefix: str,
    device: int,
    inode: int,
    generation: int,
    offset: int,
) -> str:
    return f"{prefix}:{device}:{inode}:{generation}:{offset}"


def stream_metadata(
    path: Path,
    device: int,
    inode: int,
    generation: int,
    offset: int,
    *,
    uncertain: bool = False,
    uncertainty_reason: str = "",
) -> dict[str, object]:
    path_digest = hashlib.sha256(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()
    return {
        "stream_path_sha256": path_digest,
        "stream_device": device,
        "stream_inode": inode,
        "stream_generation": generation,
        "stream_offset": offset,
        "stream_uncertain": uncertain,
        "stream_uncertainty_reason": uncertainty_reason,
    }

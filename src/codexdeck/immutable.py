"""Freeze mutable builders at the published snapshot boundary."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from typing import Any, TypeVar, cast

T = TypeVar("T")


class PublishedGuard:
    """Allow builder mutation until an object enters a published snapshot."""

    __slots__ = ("_codexdeck_published",)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_codexdeck_published", False):
            raise TypeError(f"published {type(self).__name__} is immutable")
        object.__setattr__(self, name, value)

    @property
    def is_published(self) -> bool:
        return bool(getattr(self, "_codexdeck_published", False))


class FrozenDict(dict[Any, Any]):
    """A dict-compatible mapping that rejects mutation after publication."""

    def _immutable(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("published mapping is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable  # type: ignore[assignment]
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable  # type: ignore[assignment]

    def __deepcopy__(self, _memo: dict[int, object]) -> FrozenDict:
        return self


def _frozen_dict(value: dict[object, object]) -> FrozenDict:
    frozen = FrozenDict()
    dict.update(frozen, ((freeze_value(key), freeze_value(item)) for key, item in value.items()))
    return frozen


def freeze_value(value: T) -> T:
    """Structurally clone and freeze a value reachable from a public snapshot."""

    if isinstance(value, PublishedGuard) and value.is_published:
        return value
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, dict):
        return cast(T, _frozen_dict(value))
    if isinstance(value, (list, tuple)):
        return cast(T, tuple(freeze_value(item) for item in value))
    if isinstance(value, (set, frozenset)):
        return cast(T, frozenset(freeze_value(item) for item in value))
    if is_dataclass(value) and not isinstance(value, type):
        updates = {
            item.name: freeze_value(getattr(value, item.name))
            for item in fields(value)
            if item.init
        }
        cloned = replace(value, **updates)
        if isinstance(cloned, PublishedGuard):
            object.__setattr__(cloned, "_codexdeck_published", True)
        return cast(T, cloned)
    return value

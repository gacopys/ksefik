"""Szkice faktur: zapis projektu (Draft) do JSON i z powrotem - z kwotami jako tekst, bez utraty precyzji."""

from __future__ import annotations

import types
import typing
from dataclasses import fields, is_dataclass
from datetime import date
from decimal import Decimal
from functools import cache
from typing import Any

from .invoice import Draft


def dump(value: Any) -> Any:
    """Obiekt (dataclass, Decimal, date, krotki) -> wartości JSON."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: dump(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [dump(v) for v in value]
    return value


@cache
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def _load(kind: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = typing.get_origin(kind)
    if origin in (typing.Union, types.UnionType):
        options = [k for k in typing.get_args(kind) if k is not type(None)]
        return _load(options[0], value)
    if origin is tuple:
        args = typing.get_args(kind)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_load(args[0], v) for v in value)
        return tuple(_load(k, v) for k, v in zip(args, value, strict=True))
    if is_dataclass(kind):
        hints = _hints(kind)
        return kind(**{k: _load(hints[k], v) for k, v in value.items()})
    if kind is Decimal:
        return Decimal(value)
    if kind is date:
        return date.fromisoformat(value)
    return value


def load_draft(data: dict[str, Any]) -> Draft:
    """Projekt faktury z JSON zapisanego przez `dump`."""
    return _load(Draft, data)

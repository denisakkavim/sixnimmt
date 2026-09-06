"""Validation for text and metadata that must survive JSON and UTF-8."""

from typing import Any

MAX_METADATA_DEPTH = 8


def check_representable(value: Any, depth: int = 0) -> None:
    """Refuse anything the application could not serialize into its log."""
    if depth > MAX_METADATA_DEPTH:
        msg = f"nested more than {MAX_METADATA_DEPTH} levels deep"
        raise ValueError(msg)
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as broken:
            msg = "contains text that cannot be encoded as UTF-8, such as a lone surrogate"
            raise ValueError(msg) from broken
        return
    if isinstance(value, dict):
        for key, item in value.items():
            check_representable(key, depth + 1)
            check_representable(item, depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            check_representable(item, depth + 1)

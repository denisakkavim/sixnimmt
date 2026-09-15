"""Atomic publication of complete files with an explicit durability policy."""

import json
import os
from pathlib import Path


def sync_directory(path: Path) -> None:
    """Persist a file's directory entry where the platform supports it."""
    try:
        handle = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def write_bytes(path: Path, content: bytes, *, durable: bool = True) -> None:
    """Replace a complete file; authoritative evidence also syncs file and directory."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            if durable:
                os.fsync(handle.fileno())
        temporary.replace(path)
        if durable:
            sync_directory(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value: object, *, indent: int | None = None) -> None:
    """Durably replace a JSON document, rejecting non-finite values before writing."""
    separators = (",", ":") if indent is None else None
    content = json.dumps(value, indent=indent, separators=separators, sort_keys=True, allow_nan=False) + "\n"
    write_bytes(path, content.encode("utf-8"))

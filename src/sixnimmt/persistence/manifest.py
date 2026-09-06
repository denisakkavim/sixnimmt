"""Versioned experiment provenance shared with trace readers."""

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class ManifestMatch(BaseModel):
    manifest_version: int = 1
    game_index: int
    match_id: str
    seed: int
    outcome: Literal["finished", "abandoned", "forfeited", "failed"]
    winners: tuple[str, ...] = ()
    ended_by: str | None = None
    reason: str | None = None
    log: str
    actions: str
    seat_stats: dict[str, Any] = Field(default_factory=dict)
    stats_errors: dict[str, str] = Field(default_factory=dict)


def write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    """Publish a complete manifest only after its contents are durable."""
    temporary = directory / "manifest.json.tmp"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(directory / "manifest.json")

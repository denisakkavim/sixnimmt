"""Versioned experiment provenance shared with trace readers."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from sixnimmt.common.evaluation import VersionOne
from sixnimmt.persistence.atomic import write_json


class ManifestMatch(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    manifest_version: VersionOne = 1
    game_index: int
    match_id: str
    seed: int
    outcome: Literal["finished", "abandoned", "forfeited", "failed"]
    winners: tuple[str, ...] = ()
    ended_by: str | None = None
    reason: str | None = None
    log: str
    actions: str
    seat_stats: dict[str, JsonValue] = Field(default_factory=dict)
    stats_errors: dict[str, str] = Field(default_factory=dict)


def write_manifest(directory: Path, manifest: dict[str, JsonValue]) -> None:
    """Publish a complete manifest only after its contents are durable."""
    write_json(directory / "manifest.json", manifest, indent=2)


class _TraceManifest(BaseModel):
    manifest_version: VersionOne
    matches: tuple[ManifestMatch, ...]


def read_manifest_entry(path: Path, log_name: str) -> ManifestMatch:
    """Return the sole recorded match for a trace from a supported manifest."""
    manifest = _TraceManifest.model_validate_json(path.read_text(encoding="utf-8"))
    entries = [entry for entry in manifest.matches if entry.log == log_name]
    if len(entries) != 1:
        msg = "manifest must contain exactly one entry for this log"
        raise ValueError(msg)
    return entries[0]

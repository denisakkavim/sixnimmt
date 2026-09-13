"""Durable JSON containers for plans, outcomes and execution status."""

import json
import os
from pathlib import Path
from typing import Any

from sixnimmt.persistence.sink import AtomicJsonlWriter, _sync_directory


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, separators=(",", ":"), sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    _sync_directory(path)


class ArenaArtifactWriter:
    """Persist JSON payloads without depending on the arena's domain models."""

    def __init__(
        self, directory: Path, plan: dict[str, Any], status: dict[str, Any], provenance: dict[str, Any]
    ) -> None:
        directory.mkdir(parents=True, exist_ok=False)
        self.directory = directory
        self.plan = plan
        self.provenance = provenance
        _write_json(directory / "plan.json", plan)
        self.write_status_payload(status)
        self._results = AtomicJsonlWriter(directory / "results.jsonl")

    def append_payload(self, result: dict[str, Any]) -> None:
        self._results.write([result])

    def write_status_payload(self, status: dict[str, Any]) -> None:
        _write_json(
            self.directory / "manifest.json",
            {
                "artifact_kind": "arena_run",
                "manifest_version": 1,
                "plan_id": self.plan["plan_id"],
                "files": {
                    "plan": "plan.json",
                    "results": "results.jsonl",
                    "analysis": "analysis.json.gz",
                    "report": "report.md",
                    "trace_directory": self.provenance["trace_directory"],
                },
                "status": status,
                "provenance": self.provenance,
            },
        )

    def close(self) -> None:
        self._results.close()

    def write_trace_manifest(self, manifest: dict[str, Any]) -> None:
        directory = self.directory / "traces"
        directory.mkdir(exist_ok=True)
        _write_json(directory / "manifest.json", manifest)

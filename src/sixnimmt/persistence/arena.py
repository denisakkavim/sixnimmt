"""Durable JSON containers for plans, outcomes and execution status."""

from pathlib import Path

from pydantic import JsonValue

from sixnimmt.persistence.atomic import write_json
from sixnimmt.persistence.sink import AtomicJsonlWriter


class ArenaArtifactWriter:
    """Persist JSON payloads without depending on the arena's domain models."""

    def __init__(
        self,
        directory: Path,
        plan: dict[str, JsonValue],
        status: dict[str, JsonValue],
        provenance: dict[str, JsonValue],
    ) -> None:
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.directory = directory
        self.plan = plan
        self.provenance = provenance
        write_json(directory / "plan.json", plan)
        self.write_status_payload(status)
        self._results = AtomicJsonlWriter(directory / "results.jsonl")

    def append_payload(self, result: dict[str, JsonValue]) -> None:
        self._results.write([result])

    def write_status_payload(self, status: dict[str, JsonValue]) -> None:
        write_json(
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

    def write_trace_manifest(self, manifest: dict[str, JsonValue]) -> None:
        directory = self.directory / "traces"
        directory.mkdir(mode=0o700, exist_ok=True)
        write_json(directory / "manifest.json", manifest)

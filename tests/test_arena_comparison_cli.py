"""The arena command retains comparison evidence and renders reusable reports."""

import gzip
import json
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

from sixnimmt.analytics.evaluation import analyse_run
from sixnimmt.arena.artifacts import load_run
from sixnimmt.arena.bots.base import Bot, BotSpec
from sixnimmt.arena.bots.registry import REGISTRY
from sixnimmt.cli import app

runner = CliRunner()


def _failing_factory(seed: int) -> Bot:
    msg = "cannot construct configured bot"
    raise RuntimeError(msg)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({
            "catalogue": [
                {"key": "low", "bot": "lowest_card"},
                {"key": "gap", "bot": "closest_gap"},
            ],
            "player_counts": [4],
            "games": 2,
            "seed": 333,
            "protocol": {"end_condition": "fixed_hands", "hands": 1},
            "execution": {"concurrency": 2},
            "analysis": {"bootstrap_samples": 20},
        }),
        encoding="utf-8",
    )
    return path


def test_comparison_command_saves_reanalysable_evidence(config_file: Path, tmp_path: Path) -> None:
    directory = tmp_path / "results"
    result = runner.invoke(app, ["arena", "--config", str(config_file), "--output-dir", str(directory)])
    assert result.exit_code == 0, result.output
    assert {path.name for path in directory.iterdir()} == {
        "plan.json",
        "results.jsonl",
        "manifest.json",
        "analysis.json.gz",
        "report.md",
    }
    run = load_run(directory)
    assert len(run.results) == 2
    assert run.plan.execution.concurrency == 2
    assert run.plan.seed == 333
    saved_analysis = gzip.decompress((directory / "analysis.json.gz").read_bytes())
    assert json.loads(analyse_run(run).model_dump_json()) == json.loads(saved_analysis)
    assert "report.md" in result.stdout
    assert str(directory) in result.stderr


@pytest.mark.parametrize("backend", ["thread", "process"])
def test_interactive_arena_animates_progress_across_all_table_sizes(
    config_file: Path, tmp_path: Path, backend: str
) -> None:
    result = runner.invoke(
        app,
        [
            "arena",
            "--config",
            str(config_file),
            "--output-dir",
            str(tmp_path / "results"),
            "--player-count",
            "3",
            "--player-count",
            "5",
            "--backend",
            backend,
        ],
        env={"TTY_COMPATIBLE": "1", "TERM": "xterm", "COLUMNS": "100", "LINES": "50"},
    )
    assert result.exit_code == 0, result.output
    output = Text.from_ansi(result.stdout).plain
    assert "The bull pen" in output
    assert "The statistics stable" in output
    assert output.index("The bull pen") < output.index("The statistics stable") < output.index("Arena results")
    assert "Matches completed: 4/4" in output
    assert "3 players at the doodle table" in output
    assert "\x1b[?25h" in result.stdout


@pytest.mark.parametrize("arguments", [["--no-animation"], ["--json"]])
def test_interactive_arena_can_omit_animation(config_file: Path, tmp_path: Path, arguments: list[str]) -> None:
    result = runner.invoke(
        app,
        ["arena", "--config", str(config_file), "--output-dir", str(tmp_path / "results"), *arguments],
        env={"TTY_COMPATIBLE": "1", "TERM": "xterm"},
    )
    assert result.exit_code == 0, result.output
    assert "The bull pen" not in result.stdout
    assert "The statistics stable" not in result.stdout
    assert "\x1b[?25l" not in result.stdout
    if "--json" in arguments:
        assert json.loads(result.stdout)["report"]["diagnostics"]["finished_matches"] == 2


@pytest.mark.parametrize("json_output", [False, True])
def test_without_output_directory_runs_without_saving_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, json_output: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    arguments = ["arena", "--games", "2", "--no-animation"]
    if json_output:
        arguments.append("--json")
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.output
    assert list(tmp_path.iterdir()) == []
    assert "Saving results" not in result.stderr
    if json_output:
        payload = json.loads(result.stdout)
        assert payload["artifacts"] is None
        assert payload["report"]["artifact_dir"] is None
        assert payload["report"]["diagnostics"]["finished_matches"] == 2
    else:
        assert "Arena results" in result.stdout
        assert "Full report" not in result.stdout


def test_trace_requires_an_explicit_output_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["arena", "--games", "1", "--trace"])
    assert result.exit_code == 2
    assert "--trace requires --output-dir" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_explicit_command_options_override_saved_settings(config_file: Path, tmp_path: Path) -> None:
    directory = tmp_path / "results"
    result = runner.invoke(
        app,
        [
            "arena",
            "--config",
            str(config_file),
            "--output-dir",
            str(directory),
            "--seed",
            "444",
            "--concurrency",
            "1",
            "--games",
            "1",
            "--player-count",
            "5",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "report" in payload
    assert payload["artifacts"]["directory"] == str(directory)
    run = load_run(directory)
    assert run.plan.seed == 444
    assert run.plan.execution.concurrency == 1
    assert len(run.results) == 1


def test_catalogue_command_uses_comparison_sampling(tmp_path: Path) -> None:
    catalogue = tmp_path / "config.json"
    catalogue.write_text('{"catalogue":[{"bot": "lowest_card"}]}', encoding="utf-8")
    directory = tmp_path / "results"
    result = runner.invoke(
        app,
        [
            "arena",
            "--config",
            str(catalogue),
            "--player-count",
            "4",
            "--games",
            "1",
            "--output-dir",
            str(directory),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["report"]["diagnostics"]["finished_matches"] == 1


def test_failed_matches_are_reported_without_competitive_scores(config_file: Path, tmp_path: Path) -> None:
    directory = tmp_path / "results"
    result = runner.invoke(
        app,
        [
            "arena",
            "--config",
            str(config_file),
            "--output-dir",
            str(directory),
            "--match-action-limit",
            "1",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)["report"]
    assert report["diagnostics"]["finished_matches"] == 0
    assert report["diagnostics"]["unsuccessful_matches"] == 2
    assert all(record.scores is None for record in load_run(directory).results)


def test_invalid_lineup_settings_do_not_create_run_directory(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text('{"games": -1}', encoding="utf-8")
    directory = tmp_path / "results"
    result = runner.invoke(app, ["arena", "--config", str(path), "--output-dir", str(directory)])
    assert result.exit_code == 2
    assert not directory.exists()


def test_existing_artifacts_are_preserved(config_file: Path, tmp_path: Path) -> None:
    directory = tmp_path / "results"
    directory.mkdir()
    marker = directory / "keep.txt"
    marker.write_text("existing evidence", encoding="utf-8")
    result = runner.invoke(app, ["arena", "--config", str(config_file), "--output-dir", str(directory)])
    assert result.exit_code == 1
    assert marker.read_text(encoding="utf-8") == "existing evidence"
    assert str(directory) in result.stderr


def test_stopped_plan_saves_report_and_returns_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(REGISTRY, "broken", BotSpec("broken", _failing_factory, True, {}))
    catalogue = tmp_path / "config.json"
    catalogue.write_text('{"catalogue":[{"bot": "broken"}]}', encoding="utf-8")
    directory = tmp_path / "results"
    result = runner.invoke(
        app,
        [
            "arena",
            "--config",
            str(catalogue),
            "--player-count",
            "4",
            "--games",
            "2",
            "--stop-on-failure",
            "--output-dir",
            str(directory),
            "--json",
        ],
    )
    assert result.exit_code == 1
    report = json.loads(result.stdout)["report"]
    assert report["run_status"] == "stopped"
    assert report["diagnostics"]["returned_matches"] == 1
    assert report["diagnostics"]["missing_matches"] == 1
    assert (directory / "report.md").is_file()

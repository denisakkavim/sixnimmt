"""The run CLI saves coherent evidence for its requested schedule."""

import json
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner, Result

from sixnimmt.arena.artifacts import load_run
from sixnimmt.cli import app

runner = CliRunner()


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text('{"catalogue":[{"bot": "random"}, {"bot": "lowest_card"}]}', encoding="utf-8")
    return path


def invoke_run(config_file: Path, output_dir: Path, *arguments: str) -> Result:
    return runner.invoke(
        app,
        [
            "play",
            "--config",
            str(config_file),
            "--games",
            "2",
            "--seed",
            "1234",
            "--output-dir",
            str(output_dir),
            *arguments,
        ],
    )


def test_run_help_describes_games_and_one_output_location() -> None:
    result = runner.invoke(app, ["play", "--help"], env={"COLUMNS": "120"})
    assert result.exit_code == 0
    help_text = Text.from_ansi(result.stdout).plain
    for option in ("--games", "--player-count", "--config", "--output-dir", "--trace"):
        assert option in help_text
    assert "blocks" not in help_text
    assert "--players-file" not in help_text
    assert "--trace-dir" not in help_text


@pytest.mark.parametrize("player_count", [2, 3, 4, 5, 6, 7, 8, 9, 10])
def test_plays_exact_game_count_for_every_supported_table_size(
    config_file: Path, tmp_path: Path, player_count: int
) -> None:
    directory = tmp_path / "run"
    result = invoke_run(config_file, directory, "--player-count", str(player_count), "--games", "3", "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)["report"]
    assert report["diagnostics"]["finished_matches"] == 3
    run = load_run(directory)
    assert len(run.results) == 3
    assert all(len(record.seats) == player_count for record in run.results)


def test_default_run_uses_four_players_and_reference_catalogue(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    result = runner.invoke(app, ["play", "--games", "1", "--output-dir", str(directory), "--json"])
    assert result.exit_code == 0, result.output
    run = load_run(directory)
    assert len(run.plan.catalogue) == 11
    assert run.plan.player_counts == (4,)
    assert len(run.results) == 1


def test_selected_table_sizes_each_receive_requested_games(config_file: Path, tmp_path: Path) -> None:
    directory = tmp_path / "run"
    result = invoke_run(config_file, directory, "--player-count", "3", "--player-count", "6")
    assert result.exit_code == 0, result.output
    run = load_run(directory)
    assert len(run.results) == 4
    assert [job.player_count for job in run.plan.jobs].count(3) == 2
    assert [job.player_count for job in run.plan.jobs].count(6) == 2


def test_process_and_thread_backends_preserve_game_results(config_file: Path, tmp_path: Path) -> None:
    first = invoke_run(config_file, tmp_path / "thread")
    second = invoke_run(config_file, tmp_path / "process", "--backend", "process", "--concurrency", "2")
    assert first.exit_code == second.exit_code == 0, second.output
    thread = load_run(tmp_path / "thread")
    process = load_run(tmp_path / "process")
    assert thread.plan.jobs == process.plan.jobs
    assert [record.scores for record in thread.results] == [record.scores for record in process.results]


@pytest.mark.parametrize("communication", [False, True])
def test_optional_traces_live_inside_output_and_support_inspection(
    config_file: Path, tmp_path: Path, communication: bool
) -> None:
    directory = tmp_path / "run"
    arguments = ["--trace", "--games", "1"]
    if communication:
        arguments.append("--communication")
    result = invoke_run(config_file, directory, *arguments)
    assert result.exit_code == 0, result.output
    run = load_run(directory)
    trace = run.results[0].event_trace
    assert trace is not None
    path = directory / trace
    assert path.parent == directory / "traces"
    assert (directory / "plan.json").is_file()
    assert (directory / "results.jsonl").is_file()
    assert (directory / "manifest.json").is_file()
    assert (path.parent / "manifest.json").is_file()
    summary = runner.invoke(app, ["summarise", str(path)])
    assert summary.exit_code == 0, summary.output
    assert json.loads(summary.stdout)["has_manifest"] is True
    replay = runner.invoke(app, ["replay", str(path)])
    assert replay.exit_code == 0, replay.output
    assert "status=finished" in replay.stdout


def test_without_trace_keeps_compact_evidence(config_file: Path, tmp_path: Path) -> None:
    directory = tmp_path / "run"
    result = invoke_run(config_file, directory)
    assert result.exit_code == 0, result.output
    assert (directory / "results.jsonl").is_file()
    assert not (directory / "traces").exists()
    assert "blocks" not in result.stdout.lower()


@pytest.mark.parametrize(
    "contents, message",
    [
        ('{"catalogue":["random"]}', "catalogue.0"),
        ('{"catalogue":[{"bot": "unknown"}]}', "unknown bot"),
        ('{"catalogue":[{"bot": "random", "options": {"typo": 1}}]}', "invalid options"),
        ('{"catalogue":[{"bot": "random", "display_name": "Alice"}]}', "Extra inputs"),
        ("not json", "Invalid JSON"),
    ],
)
def test_rejects_invalid_catalogue(config_file: Path, tmp_path: Path, contents: str, message: str) -> None:
    config_file.write_text(contents, encoding="utf-8")
    result = invoke_run(config_file, tmp_path / "run")
    assert result.exit_code == 2
    assert message in result.stderr
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--player-count", "1"],
        ["--player-count", "11"],
        ["--games", "-1"],
        ["--match-action-limit", "0"],
        ["--backend", "unknown"],
        ["--communication", "--scheduler", "sequential"],
    ],
)
def test_rejects_invalid_execution_settings(config_file: Path, tmp_path: Path, arguments: list[str]) -> None:
    result = invoke_run(config_file, tmp_path / "run", *arguments)
    assert result.exit_code == 2
    assert not (tmp_path / "run").exists()


def test_zero_games_requires_another_schedule(config_file: Path, tmp_path: Path) -> None:
    result = invoke_run(config_file, tmp_path / "run", "--games", "0")
    assert result.exit_code == 2


def test_labels_are_printed_literally(config_file: Path, tmp_path: Path) -> None:
    config_file.write_text('{"catalogue":[{"bot": "random", "label": "[red]Random[/red]"}]}', encoding="utf-8")
    result = invoke_run(config_file, tmp_path / "run")
    assert result.exit_code == 0, result.output
    assert "[red]Random[/red]" in result.stdout


def test_root_help_lists_one_run_command_without_legacy_aliases() -> None:
    result = runner.invoke(app, ["--help"], env={"COLUMNS": "120"})
    assert result.exit_code == 0
    help_text = Text.from_ansi(result.stdout).plain
    assert " play " in help_text
    assert " run " not in help_text
    assert " arena " not in help_text
    assert " table " not in help_text


@pytest.mark.parametrize("command", ["arena", "table", "run"])
def test_removed_command_aliases_are_rejected(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 2
    assert f"No such command '{command}'" in result.stderr
